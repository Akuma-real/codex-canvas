from __future__ import annotations

import codecs
import os
import selectors
import shutil
import subprocess
import textwrap
import time
from collections.abc import Callable, Iterable
from datetime import datetime
from pathlib import Path

from .models import GenerationPhase, GenerationRequest, GenerationResult, ReferenceImage

IMAGE_ACTIONS = ("auto", "generate", "edit")
OUTPUT_FORMATS = ("png", "jpeg", "webp")
BACKGROUNDS = ("auto", "opaque", "transparent")
SIZES = ("auto", "1024x1024", "1536x1024", "1024x1536")
QUALITIES = ("auto", "low", "medium", "high")
DEFAULT_OUTPUT_DIR = "./generated_images"
IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp"})
NEW_FILE_GRACE_NS = 5_000_000_000
LOG_IDLE_HINT_SECONDS = 8.0
LOG_IDLE_REPEAT_SECONDS = 10.0

PhaseCallback = Callable[[GenerationPhase], None]
LogCallback = Callable[[str, str], None]
StatusCallback = Callable[[str], None]


def get_codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))


def get_generated_images_dir() -> Path:
    return get_codex_home() / "generated_images"


def normalize_output_dir(
    raw_output_dir: str,
    default_output_dir: str = DEFAULT_OUTPUT_DIR,
) -> Path:
    value = (raw_output_dir or "").strip() or default_output_dir
    return Path(value).expanduser().resolve()


def validate_request(
    prompt: str,
    size: str,
    quality: str,
    output_dir: str,
    *,
    reference_images: Iterable[ReferenceImage] = (),
    primary_reference_image_id: str | None = None,
    image_action: str = "auto",
    output_format: str = "png",
    background: str = "auto",
    compression: str | int | None = None,
    default_output_dir: str = DEFAULT_OUTPUT_DIR,
) -> GenerationRequest:
    cleaned_prompt = prompt.strip()
    if not cleaned_prompt:
        raise ValueError("Prompt 不能为空。")

    if size not in SIZES:
        raise ValueError(f"不支持的尺寸：{size}")
    if quality not in QUALITIES:
        raise ValueError(f"不支持的质量：{quality}")

    validated_reference_images = tuple(reference_images)
    reference_ids = [image.id for image in validated_reference_images]
    if len(set(reference_ids)) != len(reference_ids):
        raise ValueError("参考图 ID 不能重复。")
    if validated_reference_images and primary_reference_image_id is None:
        raise ValueError("存在参考图时必须指定主参考图。")
    if primary_reference_image_id is not None and primary_reference_image_id not in set(
        reference_ids
    ):
        raise ValueError("主参考图必须存在于参考图列表中。")
    if not validated_reference_images and primary_reference_image_id is not None:
        raise ValueError("没有参考图时不能指定主参考图。")

    if image_action not in IMAGE_ACTIONS:
        raise ValueError(f"不支持的 Action：{image_action}")
    if image_action == "edit" and not validated_reference_images:
        raise ValueError("Action 为 edit 时至少需要 1 张参考图。")

    if output_format not in OUTPUT_FORMATS:
        raise ValueError(f"不支持的 Format：{output_format}")
    if background not in BACKGROUNDS:
        raise ValueError(f"不支持的 Background：{background}")

    normalized_compression = normalize_compression(compression)
    if normalized_compression is not None and output_format not in {"jpeg", "webp"}:
        raise ValueError("Compression 仅支持 jpeg 或 webp 格式。")

    return GenerationRequest(
        prompt=cleaned_prompt,
        size=size,
        quality=quality,
        output_dir=normalize_output_dir(output_dir, default_output_dir),
        reference_images=validated_reference_images,
        primary_reference_image_id=primary_reference_image_id,
        image_action=image_action,
        output_format=output_format,
        background=background,
        compression=normalized_compression,
    )


def normalize_compression(raw_compression: str | int | None) -> int | None:
    if raw_compression is None:
        return None
    if isinstance(raw_compression, int):
        value = raw_compression
    else:
        cleaned = raw_compression.strip()
        if not cleaned:
            return None
        try:
            value = int(cleaned)
        except ValueError as exc:
            raise ValueError("Compression 必须是 0-100 的整数。") from exc

    if not 0 <= value <= 100:
        raise ValueError("Compression 必须在 0-100 之间。")
    return value


def list_image_files(root: Path) -> list[Path]:
    if not root.exists():
        return []
    files: list[Path] = []
    for path in root.rglob("*"):
        if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES:
            files.append(path)
    return files


def snapshot_image_mtimes(files: Iterable[Path]) -> dict[str, int]:
    snapshot: dict[str, int] = {}
    for path in files:
        try:
            snapshot[str(path.resolve())] = path.stat().st_mtime_ns
        except OSError:
            continue
    return snapshot


def build_codex_prompt(request: GenerationRequest) -> str:
    reference_lines = [f"- 本次共有 {len(request.reference_images)} 张参考图。"]
    if request.reference_images:
        reference_lines.append("- 第 1 张参考图是主参考图，优先决定主体、构图与风格。")
        if len(request.reference_images) > 1:
            reference_lines.append(
                f"- 其余 {len(request.reference_images) - 1} 张为补充参考图，仅作补充参考。"
            )
    else:
        reference_lines.append("- 本次没有参考图，请仅根据文本提示词生成。")

    parameter_lines = [
        f"- action: {request.image_action}",
        f"- format: {request.output_format}",
        f"- background: {request.background}",
        f"- size: {request.size}",
        f"- quality: {request.quality}",
    ]
    if request.compression is not None:
        parameter_lines.insert(3, f"- compression: {request.compression}")

    return textwrap.dedent(
        f"""
        $imagegen
        请生成 1 张图片，不要生成代码，不要创建额外文档。
        使用内置图片生成功能。
        除通过 --image 传入的参考图外，其余图像参数都属于提示性约束，请尽量满足。

        参考图说明：
        {chr(10).join(reference_lines)}

        图像参数：
        {chr(10).join(parameter_lines)}

        要求：
        - 尽量按以下用户提示词生成
        - 若无特殊必要，不要添加文字、水印、边框

        用户提示词：
        {request.prompt}
        """
    ).strip()


def order_reference_images(request: GenerationRequest) -> tuple[ReferenceImage, ...]:
    if not request.reference_images or request.primary_reference_image_id is None:
        return request.reference_images

    primary_image: ReferenceImage | None = None
    supplemental_images: list[ReferenceImage] = []
    for image in request.reference_images:
        if image.id == request.primary_reference_image_id:
            primary_image = image
        else:
            supplemental_images.append(image)

    if primary_image is None:
        return request.reference_images
    return (primary_image, *supplemental_images)


def build_codex_command(codex_path: str, request: GenerationRequest) -> tuple[str, ...]:
    command: list[str] = [
        codex_path,
        "exec",
        "--skip-git-repo-check",
        "--color",
        "never",
    ]
    for reference_image in order_reference_images(request):
        command.extend(["--image", str(reference_image.path)])
    command.extend(["--", build_codex_prompt(request)])
    return tuple(command)


def find_newest_generated_image(
    generated_images_dir: Path,
    before: dict[str, int],
    command_started_ns: int,
    grace_ns: int = NEW_FILE_GRACE_NS,
) -> Path:
    candidates: list[tuple[int, Path]] = []
    for path in list_image_files(generated_images_dir):
        try:
            resolved = str(path.resolve())
            mtime_ns = path.stat().st_mtime_ns
        except OSError:
            continue
        if resolved not in before or mtime_ns > before.get(resolved, 0):
            candidates.append((mtime_ns, path))

    if not candidates:
        threshold = command_started_ns - grace_ns
        for path in list_image_files(generated_images_dir):
            try:
                mtime_ns = path.stat().st_mtime_ns
            except OSError:
                continue
            if mtime_ns >= threshold:
                candidates.append((mtime_ns, path))

    if not candidates:
        raise RuntimeError(
            "Codex 执行成功，但没有在 $CODEX_HOME/generated_images 中找到新图片。\n"
            "请检查 Codex 是否真的调用了图片生成，或查看 ~/.codex/generated_images。"
        )

    candidates.sort(key=lambda item: (item[0], str(item[1])), reverse=True)
    return candidates[0][1]


def build_failure_summary(return_code: int, stdout: str, stderr: str) -> str:
    primary = stderr or stdout
    if primary:
        return primary
    return f"codex exec 失败，返回码 {return_code}。"


def stream_process_output(
    proc: subprocess.Popen[bytes],
    *,
    perf_counter: Callable[[], float],
    log_callback: LogCallback | None = None,
    status_callback: StatusCallback | None = None,
) -> tuple[str, str, int]:
    selector = selectors.DefaultSelector()
    stdout_parts: list[str] = []
    stderr_parts: list[str] = []
    streams = {
        "stdout": proc.stdout,
        "stderr": proc.stderr,
    }
    decoders = {
        "stdout": codecs.getincrementaldecoder("utf-8")(errors="replace"),
        "stderr": codecs.getincrementaldecoder("utf-8")(errors="replace"),
    }

    for stream_name, pipe in streams.items():
        if pipe is not None:
            selector.register(pipe, selectors.EVENT_READ, stream_name)

    last_output_at = perf_counter()
    last_idle_hint_at = 0.0
    if status_callback is not None:
        status_callback("已启动 codex exec，等待命令输出。")

    while selector.get_map():
        events = selector.select(timeout=0.5)
        if events:
            for key, _ in events:
                stream_name = str(key.data)
                pipe = key.fileobj
                read_chunk = getattr(pipe, "read1", pipe.read)
                chunk = read_chunk(4096)
                if not chunk:
                    selector.unregister(pipe)
                    continue
                text = decoders[stream_name].decode(chunk)
                if not text:
                    continue
                if stream_name == "stdout":
                    stdout_parts.append(text)
                else:
                    stderr_parts.append(text)
                last_output_at = perf_counter()
                last_idle_hint_at = 0.0
                if log_callback is not None:
                    log_callback(stream_name, text)
        else:
            idle_for = perf_counter() - last_output_at
            if (
                status_callback is not None
                and idle_for >= LOG_IDLE_HINT_SECONDS
                and (
                    last_idle_hint_at == 0.0
                    or perf_counter() - last_idle_hint_at >= LOG_IDLE_REPEAT_SECONDS
                )
            ):
                status_callback(f"codex exec 仍在运行，已连续 {int(idle_for)} 秒没有新的输出。")
                last_idle_hint_at = perf_counter()

            if proc.poll() is not None and not selector.get_map():
                break

    for stream_name, decoder in decoders.items():
        tail = decoder.decode(b"", final=True)
        if not tail:
            continue
        if stream_name == "stdout":
            stdout_parts.append(tail)
        else:
            stderr_parts.append(tail)
        if log_callback is not None:
            log_callback(stream_name, tail)

    exit_code = proc.wait()
    return "".join(stdout_parts).strip(), "".join(stderr_parts).strip(), exit_code


def run_generation(
    request: GenerationRequest,
    *,
    codex_which: Callable[[str], str | None] = shutil.which,
    subprocess_popen: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
    generated_images_dir_factory: Callable[[], Path] = get_generated_images_dir,
    now_factory: Callable[[], datetime] = datetime.now,
    perf_counter: Callable[[], float] = time.perf_counter,
    time_ns: Callable[[], int] = time.time_ns,
    copy_file: Callable[[Path, Path], str] = shutil.copy2,
    progress_callback: PhaseCallback | None = None,
    log_callback: LogCallback | None = None,
    status_callback: StatusCallback | None = None,
) -> GenerationResult:
    phase = GenerationPhase.VALIDATING_INPUT
    stdout = ""
    stderr = ""
    command: tuple[str, ...] = ()
    copied_to: Path | None = None
    original_file: Path | None = None
    exit_code: int | None = None
    started = perf_counter()

    def advance(next_phase: GenerationPhase) -> None:
        nonlocal phase
        phase = next_phase
        if progress_callback is not None:
            progress_callback(next_phase)

    advance(GenerationPhase.VALIDATING_INPUT)

    try:
        codex_path = codex_which("codex")
        if not codex_path:
            raise RuntimeError("未找到 codex 命令，请先安装 Codex CLI，并确认它在 PATH 中。")

        request.output_dir.mkdir(parents=True, exist_ok=True)
        generated_images_dir = generated_images_dir_factory()
        before = snapshot_image_mtimes(list_image_files(generated_images_dir))
        command = build_codex_command(codex_path, request)

        advance(GenerationPhase.RUNNING_CODEX)
        command_started_ns = time_ns()
        proc = subprocess_popen(
            list(command),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        stdout, stderr, exit_code = stream_process_output(
            proc,
            perf_counter=perf_counter,
            log_callback=log_callback,
            status_callback=status_callback,
        )

        if exit_code != 0:
            raise RuntimeError(build_failure_summary(exit_code, stdout, stderr))

        advance(GenerationPhase.SCANNING_NEW_IMAGE)
        original_file = find_newest_generated_image(
            generated_images_dir,
            before,
            command_started_ns,
        )

        advance(GenerationPhase.COPYING_OUTPUT)
        timestamp = now_factory().strftime("%Y%m%d-%H%M%S")
        suffix = original_file.suffix.lower() or ".png"
        copied_to = request.output_dir / (
            f"codexcanvas_{request.size}_{request.quality}_{timestamp}{suffix}"
        )
        copy_file(original_file, copied_to)

        advance(GenerationPhase.PRESENTING_RESULT)
        summary = "图片已复制到输出目录。"
        success = True
    except Exception as exc:  # noqa: BLE001
        summary = str(exc)
        success = False

    return GenerationResult(
        request=request,
        success=success,
        phase=phase,
        summary=summary,
        elapsed_seconds=max(0.0, perf_counter() - started),
        copied_to=copied_to,
        original_file=original_file,
        stdout=stdout,
        stderr=stderr,
        command=command,
        exit_code=exit_code,
    )
