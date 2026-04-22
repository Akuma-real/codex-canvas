from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from codex_canvas.models import GenerationRequest, ReferenceImage
from codex_canvas.runner import (
    build_codex_command,
    build_codex_prompt,
    find_newest_generated_image,
    stream_process_output,
    validate_request,
)


class FakePipe:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = list(chunks)

    def read(self, _: int = -1) -> bytes:
        if self._chunks:
            return self._chunks.pop(0)
        return b""


class FakeProc:
    def __init__(
        self,
        *,
        stdout_chunks: list[bytes],
        stderr_chunks: list[bytes],
        exit_code: int,
    ) -> None:
        self.stdout = FakePipe(stdout_chunks)
        self.stderr = FakePipe(stderr_chunks)
        self._exit_code = exit_code

    def poll(self) -> None:
        return None

    def wait(self) -> int:
        return self._exit_code


class FakeSelector:
    event_names: list[list[str]] = []

    def __init__(self) -> None:
        self._registered: dict[str, FakePipe] = {}
        self._events = iter(self.event_names)

    def register(self, pipe: FakePipe, _events: int, data: str) -> None:
        self._registered[data] = pipe

    def select(self, timeout: float = 0.5) -> list[tuple[SimpleNamespace, int]]:
        del timeout
        try:
            names = next(self._events)
        except StopIteration:
            return []
        return [
            (SimpleNamespace(fileobj=self._registered[name], data=name), 1)
            for name in names
            if name in self._registered
        ]

    def unregister(self, pipe: FakePipe) -> None:
        for name, current in tuple(self._registered.items()):
            if current is pipe:
                del self._registered[name]
                break

    def get_map(self) -> dict[str, FakePipe]:
        return dict(self._registered)


def make_reference_image(tmp_path: Path, image_id: str, filename: str) -> ReferenceImage:
    path = tmp_path / filename
    path.write_bytes(b"png")
    return ReferenceImage(
        id=image_id,
        path=path,
        created_at=datetime(2026, 4, 22, 12, 0, 0),
    )


def test_validate_request_rejects_empty_prompt() -> None:
    with pytest.raises(ValueError, match="Prompt 不能为空"):
        validate_request("   ", "1024x1024", "high", "")


def test_validate_request_rejects_invalid_size() -> None:
    with pytest.raises(ValueError, match="不支持的尺寸"):
        validate_request("sunset", "999x999", "high", "")


def test_validate_request_rejects_invalid_quality() -> None:
    with pytest.raises(ValueError, match="不支持的质量"):
        validate_request("sunset", "1024x1024", "ultra", "")


def test_validate_request_requires_primary_reference_image(tmp_path: Path) -> None:
    reference_image = make_reference_image(tmp_path, "ref-1", "primary.png")

    with pytest.raises(ValueError, match="必须指定主参考图"):
        validate_request(
            "sunset",
            "1024x1024",
            "high",
            "",
            reference_images=[reference_image],
        )


def test_validate_request_requires_reference_image_for_edit_action() -> None:
    with pytest.raises(ValueError, match="edit 时至少需要 1 张参考图"):
        validate_request(
            "portrait",
            "1024x1024",
            "high",
            "",
            image_action="edit",
        )


def test_validate_request_rejects_out_of_range_compression() -> None:
    with pytest.raises(ValueError, match="Compression 必须在 0-100 之间"):
        validate_request(
            "sunset",
            "1024x1024",
            "high",
            "",
            output_format="jpeg",
            compression="101",
        )


def test_validate_request_rejects_compression_for_png() -> None:
    with pytest.raises(ValueError, match="Compression 仅支持 jpeg 或 webp"):
        validate_request(
            "sunset",
            "1024x1024",
            "high",
            "",
            output_format="png",
            compression="80",
        )


@pytest.mark.parametrize(
    ("field_name", "kwargs", "pattern"),
    [
        ("action", {"image_action": "blend"}, "不支持的 Action"),
        ("format", {"output_format": "gif"}, "不支持的 Format"),
        ("background", {"background": "alpha"}, "不支持的 Background"),
        ("size", {"size": "640x640"}, "不支持的尺寸"),
        ("quality", {"quality": "ultra"}, "不支持的质量"),
    ],
)
def test_validate_request_rejects_invalid_image_options(
    field_name: str,
    kwargs: dict[str, str],
    pattern: str,
) -> None:
    del field_name
    with pytest.raises(ValueError, match=pattern):
        validate_request(
            "sunset",
            kwargs.pop("size", "1024x1024"),
            kwargs.pop("quality", "high"),
            "",
            **kwargs,
        )


def test_validate_request_uses_default_output_dir(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)

    request = validate_request(" sunset ", "1024x1024", "high", "")

    assert request.prompt == "sunset"
    assert request.output_dir == (tmp_path / "generated_images").resolve()


def test_build_codex_prompt_preserves_request_details_and_reference_hints(tmp_path: Path) -> None:
    primary = make_reference_image(tmp_path, "ref-1", "primary.png")
    supplement = make_reference_image(tmp_path, "ref-2", "supplement.png")
    request = GenerationRequest(
        prompt="cinematic skyline",
        size="auto",
        quality="medium",
        output_dir=tmp_path,
        reference_images=(primary, supplement),
        primary_reference_image_id=primary.id,
        image_action="edit",
        output_format="jpeg",
        background="transparent",
        compression=80,
    )

    prompt = build_codex_prompt(request)

    assert "本次共有 2 张参考图" in prompt
    assert "第 1 张参考图是主参考图" in prompt
    assert "其余 1 张为补充参考图" in prompt
    assert "action: edit" in prompt
    assert "format: jpeg" in prompt
    assert "background: transparent" in prompt
    assert "compression: 80" in prompt
    assert "size: auto" in prompt
    assert "quality: medium" in prompt
    assert "cinematic skyline" in prompt


def test_build_codex_command_orders_primary_reference_first(tmp_path: Path) -> None:
    first = make_reference_image(tmp_path, "ref-1", "first.png")
    primary = make_reference_image(tmp_path, "ref-2", "primary.png")
    request = GenerationRequest(
        prompt="studio portrait",
        size="1024x1536",
        quality="high",
        output_dir=tmp_path,
        reference_images=(first, primary),
        primary_reference_image_id=primary.id,
        image_action="edit",
        output_format="webp",
        background="auto",
        compression=60,
    )

    command = build_codex_command("/usr/bin/codex", request)

    assert command[:5] == (
        "/usr/bin/codex",
        "exec",
        "--skip-git-repo-check",
        "--color",
        "never",
    )
    assert command[5:9] == (
        "--image",
        str(primary.path),
        "--image",
        str(first.path),
    )
    assert command[-2] == "--"
    assert "studio portrait" in command[-1]


def test_build_codex_command_omits_images_and_compression_when_not_applicable(
    tmp_path: Path,
) -> None:
    request = GenerationRequest(
        prompt="minimal icon",
        size="1024x1024",
        quality="auto",
        output_dir=tmp_path,
        image_action="generate",
        output_format="png",
        background="opaque",
    )

    command = build_codex_command("/usr/bin/codex", request)

    assert "--image" not in command
    assert command[-2] == "--"
    assert "compression:" not in command[-1]
    assert "本次共有 0 张参考图" in command[-1]


def test_find_newest_generated_image_detects_new_file(tmp_path: Path) -> None:
    existing = tmp_path / "existing.png"
    existing.write_bytes(b"old")
    os.utime(existing, ns=(1_000_000_000, 1_000_000_000))

    new_image = tmp_path / "new.webp"
    new_image.write_bytes(b"new")
    os.utime(new_image, ns=(2_000_000_000, 2_000_000_000))

    before = {str(existing.resolve()): existing.stat().st_mtime_ns}

    found = find_newest_generated_image(tmp_path, before, 2_000_000_000)

    assert found == new_image


def test_find_newest_generated_image_detects_updated_file(tmp_path: Path) -> None:
    updated = tmp_path / "updated.png"
    updated.write_bytes(b"image")
    os.utime(updated, ns=(1_000_000_000, 1_000_000_000))
    before = {str(updated.resolve()): updated.stat().st_mtime_ns}

    os.utime(updated, ns=(3_000_000_000, 3_000_000_000))

    found = find_newest_generated_image(tmp_path, before, 3_000_000_000)

    assert found == updated


def test_find_newest_generated_image_preserves_diagnostic_when_missing(tmp_path: Path) -> None:
    existing = tmp_path / "existing.png"
    existing.write_bytes(b"old")
    os.utime(existing, ns=(1_000_000_000, 1_000_000_000))
    before = {str(existing.resolve()): existing.stat().st_mtime_ns}

    with pytest.raises(RuntimeError, match=r"\$CODEX_HOME/generated_images 中找到新图片"):
        find_newest_generated_image(tmp_path, before, 10_000_000_000, grace_ns=1)


def test_stream_process_output_aggregates_stdout_and_stderr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    FakeSelector.event_names = [
        ["stdout"],
        ["stdout"],
        ["stderr"],
        ["stdout"],
        ["stderr"],
    ]
    monkeypatch.setattr("codex_canvas.runner.selectors.DefaultSelector", FakeSelector)
    proc = FakeProc(
        stdout_chunks=[b"hello ", b"world\n", b""],
        stderr_chunks=[b"oops\n", b""],
        exit_code=7,
    )
    logs: list[tuple[str, str]] = []
    clock_values = iter([0.0, 0.2, 0.4, 0.6, 0.8])

    stdout, stderr, exit_code = stream_process_output(
        proc,
        perf_counter=lambda: next(clock_values),
        log_callback=lambda stream, chunk: logs.append((stream, chunk)),
    )

    assert stdout == "hello world"
    assert stderr == "oops"
    assert exit_code == 7
    assert logs == [
        ("stdout", "hello "),
        ("stdout", "world\n"),
        ("stderr", "oops\n"),
    ]


def test_stream_process_output_emits_idle_status_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    FakeSelector.event_names = [
        ["stdout"],
        [],
        ["stdout"],
        ["stderr"],
    ]
    monkeypatch.setattr("codex_canvas.runner.selectors.DefaultSelector", FakeSelector)
    proc = FakeProc(
        stdout_chunks=[b"booting\n", b""],
        stderr_chunks=[b""],
        exit_code=0,
    )
    status_messages: list[str] = []
    clock_values = iter([0.0, 0.1, 9.1, 9.2, 9.3, 9.4])

    stdout, stderr, exit_code = stream_process_output(
        proc,
        perf_counter=lambda: next(clock_values),
        status_callback=status_messages.append,
    )

    assert stdout == "booting"
    assert stderr == ""
    assert exit_code == 0
    assert status_messages[0] == "已启动 codex exec，等待命令输出。"
    assert "连续 9 秒没有新的输出" in status_messages[1]
