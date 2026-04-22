from __future__ import annotations

import shutil
import time
from collections.abc import Callable, Sequence
from importlib.resources import files
from pathlib import Path

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Grid, Horizontal, VerticalScroll
from textual.events import Resize
from textual.widgets import (
    Button,
    Footer,
    Input,
    LoadingIndicator,
    OptionList,
    Select,
    Static,
    TextArea,
)
from textual.widgets.option_list import Option
from textual.worker import Worker, WorkerState

from .clipboard import ClipboardImageError, create_session_temp_dir, paste_clipboard_image
from .models import GenerationPhase, GenerationRequest, GenerationResult, ReferenceImage
from .runner import (
    DEFAULT_OUTPUT_DIR,
    run_generation,
    validate_request,
)

Runner = Callable[..., GenerationResult]
ClipboardPaste = Callable[[Path], ReferenceImage]
SessionDirFactory = Callable[[], Path]

PHASE_DESCRIPTIONS = {
    GenerationPhase.VALIDATING_INPUT: "正在校验参数并准备任务。",
    GenerationPhase.RUNNING_CODEX: "正在调用 codex exec，等待生成结果。",
    GenerationPhase.SCANNING_NEW_IMAGE: "正在扫描本次生成的新图片。",
    GenerationPhase.COPYING_OUTPUT: "正在复制图片到输出目录。",
    GenerationPhase.PRESENTING_RESULT: "正在整理并展示本次任务详情。",
}

PASTE_REFERENCE_BINDING = "ctrl+v,ctrl+shift+v,shift+insert,super+v"
ACTION_OPTIONS = (
    ("自动", "auto"),
    ("纯生成", "generate"),
    ("参考图编辑", "edit"),
)
FORMAT_OPTIONS = (
    ("PNG", "png"),
    ("JPEG", "jpeg"),
    ("WebP", "webp"),
)
BACKGROUND_OPTIONS = (
    ("自动", "auto"),
    ("不透明", "opaque"),
    ("透明", "transparent"),
)
SIZE_OPTIONS = (
    ("自动", "auto"),
    ("1024 x 1024", "1024x1024"),
    ("1536 x 1024", "1536x1024"),
    ("1024 x 1536", "1024x1536"),
)
QUALITY_OPTIONS = (
    ("自动", "auto"),
    ("低", "low"),
    ("中", "medium"),
    ("高", "high"),
)
REFERENCE_HELP = (
    "参考图只支持从系统图片剪贴板读取。可尝试 Ctrl+V、Ctrl+Shift+V、"
    "Shift+Insert 或 Cmd+V 触发读取，是否生效取决于终端是否把按键传给应用。"
    "主参考图优先决定主体、构图与风格，其余参考图仅作补充参考。"
)
ADVANCED_HELP = (
    "除通过 --image 传入的参考图外，其余参数都会写进 prompt 作为提示性约束，"
    "最终结果取决于 Codex 当前图像能力。"
)


def _load_css() -> str:
    return files("codex_canvas").joinpath("app.tcss").read_text(encoding="utf-8")


def truncate_block(content: str, *, max_lines: int = 10, max_chars: int = 1200) -> str:
    text = (content or "").strip()
    if not text:
        return "(暂无输出)"
    lines = text.splitlines()
    clipped = "\n".join(lines[:max_lines]).strip()
    if len(clipped) > max_chars:
        return clipped[: max_chars - 1].rstrip() + "…"
    if len(lines) > max_lines:
        return clipped + "\n…"
    return clipped


def summarize_reference_images(
    reference_images: Sequence[ReferenceImage],
    primary_reference_image_id: str | None,
) -> str:
    if not reference_images:
        return "当前无参考图。"

    primary_image = next(
        (image for image in reference_images if image.id == primary_reference_image_id),
        None,
    )
    if primary_image is None:
        return f"当前有 {len(reference_images)} 张参考图，但尚未指定主图。"

    if len(reference_images) == 1:
        return f"当前有 1 张参考图，主图：{primary_image.path.name}。"
    return (
        f"当前有 {len(reference_images)} 张参考图，主图：{primary_image.path.name}，"
        f"补充图：{len(reference_images) - 1} 张。"
    )


class CodexCanvasApp(App[None]):
    CSS = _load_css()
    TITLE = "CodexCanvas"
    SUB_TITLE = "基于本机 Codex CLI 的图像生成终端工具"
    BINDINGS = [
        Binding("ctrl+enter", "generate", "生成", key_display="Ctrl+Enter", priority=True),
        Binding("f5", "generate", "生成", key_display="F5", priority=True),
        Binding(PASTE_REFERENCE_BINDING, "paste_reference", show=False, priority=True),
        Binding("ctrl+q", "quit", "退出", key_display="Ctrl+Q", priority=True),
    ]

    def __init__(
        self,
        *,
        runner: Runner = run_generation,
        clipboard_paste: ClipboardPaste = paste_clipboard_image,
        session_dir_factory: SessionDirFactory = create_session_temp_dir,
    ) -> None:
        super().__init__()
        self._runner = runner
        self._clipboard_paste = clipboard_paste
        self._session_dir = session_dir_factory()
        self._session_dir.mkdir(parents=True, exist_ok=True)
        self._reference_images: list[ReferenceImage] = []
        self._primary_reference_image_id: str | None = None
        self._is_running = False
        self._started_at: float | None = None
        self._generation_worker: Worker[GenerationResult] | None = None
        self._stdout_buffer = ""
        self._stderr_buffer = ""

    def compose(self) -> ComposeResult:
        yield Static("CodexCanvas", id="title-bar")
        yield Static(self.SUB_TITLE, id="title-subtitle")
        with Horizontal(id="main-layout"):
            with VerticalScroll(id="form-panel", classes="panel"):
                yield Static("生成参数", classes="panel-title")
                yield Static("提示词", classes="field-label")
                yield TextArea(id="prompt-input")

                yield Static("参考图", classes="field-label section-gap")
                yield Static(REFERENCE_HELP, id="reference-help", classes="helper-text")
                yield OptionList(id="reference-list")
                with Grid(classes="reference-actions"):
                    yield Button("从剪贴板粘贴图片", id="paste-reference-button", variant="primary")
                    yield Button("设为主参考图", id="set-primary-button")
                    yield Button("移除选中图片", id="remove-reference-button", variant="warning")
                    yield Button("清空参考图", id="clear-references-button", variant="error")
                yield Static(
                    "当前没有参考图。", id="reference-feedback", classes="helper-text muted-text"
                )

                yield Static("高级图像参数", classes="field-label section-gap")
                yield Static(ADVANCED_HELP, id="advanced-help", classes="helper-text")
                yield Static("生成动作", classes="field-label")
                yield Select(
                    ACTION_OPTIONS,
                    value="auto",
                    allow_blank=False,
                    id="action-select",
                )
                yield Static("输出格式", classes="field-label")
                yield Select(
                    FORMAT_OPTIONS,
                    value="png",
                    allow_blank=False,
                    id="format-select",
                )
                yield Static("背景", classes="field-label")
                yield Select(
                    BACKGROUND_OPTIONS,
                    value="auto",
                    allow_blank=False,
                    id="background-select",
                )
                yield Static("压缩质量", classes="field-label")
                yield Input(
                    placeholder="0-100，仅 jpeg/webp 可用",
                    id="compression-input",
                    disabled=True,
                )
                yield Static("尺寸", classes="field-label")
                yield Select(
                    SIZE_OPTIONS,
                    value="1024x1024",
                    allow_blank=False,
                    id="size-select",
                )
                yield Static("质量", classes="field-label")
                yield Select(
                    QUALITY_OPTIONS,
                    value="high",
                    allow_blank=False,
                    id="quality-select",
                )
                yield Static("输出目录", classes="field-label")
                yield Input(
                    value=DEFAULT_OUTPUT_DIR,
                    placeholder=DEFAULT_OUTPUT_DIR,
                    id="output-dir-input",
                )
                yield Button("生成", id="generate-button", variant="success")
            with VerticalScroll(id="detail-panel", classes="panel"):
                yield Static("本次任务详情", classes="panel-title")
                with Horizontal(id="status-strip"):
                    yield LoadingIndicator(id="run-spinner", classes="hidden")
                    yield Static("等待生成", id="run-state")
                yield Static("阶段", classes="field-label detail-label")
                yield Static("未开始", id="phase-value", classes="detail-value")
                yield Static("耗时", classes="field-label detail-label")
                yield Static("0.0s", id="elapsed-value", classes="detail-value")
                yield Static("保存路径", classes="field-label detail-label")
                yield Static(
                    "尚未生成",
                    id="copied-to-value",
                    classes="detail-value detail-multiline",
                )
                yield Static("原始文件", classes="field-label detail-label")
                yield Static("尚未发现", id="origin-value", classes="detail-value detail-multiline")
                yield Static("参考图摘要", classes="field-label detail-label")
                yield Static(
                    "当前无参考图。",
                    id="reference-summary-value",
                    classes="detail-value detail-multiline",
                )
                yield Static("任务摘要", classes="field-label detail-label")
                yield Static(
                    "按 Ctrl+Enter 或 F5 开始生成。",
                    id="summary-value",
                    classes="detail-value detail-multiline",
                )
                yield Static("stdout 摘要", classes="field-label detail-label")
                yield Static("(暂无输出)", id="stdout-value", classes="detail-value detail-log")
                yield Static("stderr 摘要", classes="field-label detail-label")
                yield Static("(暂无输出)", id="stderr-value", classes="detail-value detail-log")
        yield Footer(show_command_palette=False)

    def on_mount(self) -> None:
        self.query_one("#prompt-input", TextArea).focus()
        self._sync_layout_mode()
        self._refresh_reference_widgets()
        self._sync_compression_input()
        self.set_interval(0.2, self._refresh_elapsed)

    def on_unmount(self) -> None:
        shutil.rmtree(self._session_dir, ignore_errors=True)

    def on_resize(self, _: Resize) -> None:
        self._sync_layout_mode()

    def action_generate(self) -> None:
        if self._is_running:
            return

        prompt_widget = self.query_one("#prompt-input", TextArea)
        size_value = self.query_one("#size-select", Select).value
        quality_value = self.query_one("#quality-select", Select).value
        output_dir_value = self.query_one("#output-dir-input", Input).value
        action_value = self.query_one("#action-select", Select).value
        format_value = self.query_one("#format-select", Select).value
        background_value = self.query_one("#background-select", Select).value
        compression_value = self.query_one("#compression-input", Input).value

        try:
            request = validate_request(
                prompt_widget.text,
                str(size_value),
                str(quality_value),
                output_dir_value,
                reference_images=self._reference_images,
                primary_reference_image_id=self._primary_reference_image_id,
                image_action=str(action_value),
                output_format=str(format_value),
                background=str(background_value),
                compression=compression_value,
            )
        except ValueError as exc:
            self._set_run_state("参数有误")
            self._update_phase(GenerationPhase.VALIDATING_INPUT)
            self._update_elapsed(0.0)
            self._update_paths(None, None)
            self._update_summary(str(exc))
            self._update_logs("", "")
            if "Compression" in str(exc):
                self.query_one("#compression-input", Input).focus()
            else:
                prompt_widget.focus()
            return

        self._start_run(request)
        self._generation_worker = self.run_worker(
            lambda: self._runner(
                request,
                progress_callback=lambda phase: self.call_from_thread(self._update_phase, phase),
                log_callback=lambda stream_name, chunk: self.call_from_thread(
                    self._append_log,
                    stream_name,
                    chunk,
                ),
                status_callback=lambda message: self.call_from_thread(
                    self._update_running_message,
                    message,
                ),
            ),
            thread=True,
            exclusive=True,
            exit_on_error=False,
        )

    def action_paste_reference(self) -> None:
        if self._is_running:
            return
        self._paste_reference_image_from_clipboard()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id
        if button_id == "generate-button":
            self.action_generate()
        elif button_id == "paste-reference-button":
            self.action_paste_reference()
        elif button_id == "set-primary-button":
            self._set_selected_as_primary_reference()
        elif button_id == "remove-reference-button":
            self._remove_selected_reference_image()
        elif button_id == "clear-references-button":
            self._clear_reference_images()

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "format-select":
            self._sync_compression_input()

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        if event.option_list.id == "reference-list":
            self._update_reference_action_state()

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        if event.worker is not self._generation_worker:
            return
        if event.state is WorkerState.SUCCESS:
            self._finish_run(event.worker.result)
        elif event.state is WorkerState.ERROR:
            error = event.worker.error
            self._finish_worker_crash(str(error) if error else "后台任务异常退出。")

    def _paste_reference_image_from_clipboard(self) -> None:
        try:
            reference_image = self._clipboard_paste(self._session_dir)
        except ClipboardImageError as exc:
            self._update_reference_feedback(str(exc))
            return

        self._reference_images.append(reference_image)
        if self._primary_reference_image_id is None:
            self._primary_reference_image_id = reference_image.id
            feedback = f"已添加参考图 {reference_image.path.name}，并将其设为主参考图。"
        else:
            feedback = f"已添加参考图 {reference_image.path.name}。"

        self._refresh_reference_widgets(selected_id=reference_image.id)
        self._update_reference_feedback(feedback)

    def _set_selected_as_primary_reference(self) -> None:
        selected_id = self._get_selected_reference_id()
        if selected_id is None or selected_id == self._primary_reference_image_id:
            return

        self._primary_reference_image_id = selected_id
        self._refresh_reference_widgets(selected_id=selected_id)

        selected_image = next(image for image in self._reference_images if image.id == selected_id)
        self._update_reference_feedback(f"已将 {selected_image.path.name} 设为主参考图。")

    def _remove_selected_reference_image(self) -> None:
        selected_id = self._get_selected_reference_id()
        if selected_id is None:
            return

        selected_index = next(
            index for index, image in enumerate(self._reference_images) if image.id == selected_id
        )
        removed_image = self._reference_images.pop(selected_index)

        if removed_image.id == self._primary_reference_image_id:
            self._primary_reference_image_id = (
                self._reference_images[0].id if self._reference_images else None
            )

        next_selected_id = None
        if self._reference_images:
            next_index = min(selected_index, len(self._reference_images) - 1)
            next_selected_id = self._reference_images[next_index].id

        self._refresh_reference_widgets(selected_id=next_selected_id)
        self._update_reference_feedback(f"已移除参考图 {removed_image.path.name}。")

    def _clear_reference_images(self) -> None:
        self._reference_images.clear()
        self._primary_reference_image_id = None
        self._refresh_reference_widgets()
        self._update_reference_feedback("已清空全部参考图。")

    def _start_run(self, request: GenerationRequest) -> None:
        self._is_running = True
        self._started_at = time.perf_counter()
        self._apply_busy_state(True)
        self._set_run_state("运行中")
        self._update_phase(GenerationPhase.VALIDATING_INPUT)
        self._update_elapsed(0.0)
        self._update_paths(None, None)
        self._update_summary(
            f"已接收本次任务，将传入 {len(request.reference_images)} 张参考图，"
            f"输出目录将写入到 {request.output_dir}。"
        )
        self._stdout_buffer = ""
        self._stderr_buffer = ""
        self._update_logs(
            "（尚未收到 stdout 输出，命令启动后会在这里实时显示）",
            "（尚未收到 stderr 输出）",
        )

    def _finish_run(self, result: GenerationResult) -> None:
        self._generation_worker = None
        self._is_running = False
        self._started_at = None
        self._apply_busy_state(False)
        self._set_run_state("生成成功" if result.success else "生成失败")
        self._update_phase(result.phase)
        self._update_elapsed(result.elapsed_seconds)
        self._update_paths(result.copied_to, result.original_file)
        self._update_summary(result.summary)
        self._update_logs(result.stdout, result.stderr)

    def _finish_worker_crash(self, error_message: str) -> None:
        self._generation_worker = None
        self._is_running = False
        self._started_at = None
        self._apply_busy_state(False)
        self._set_run_state("后台异常")
        self._update_summary(error_message or "后台任务异常退出。")

    def _apply_busy_state(self, running: bool) -> None:
        prompt = self.query_one("#prompt-input", TextArea)
        action = self.query_one("#action-select", Select)
        format_select = self.query_one("#format-select", Select)
        background = self.query_one("#background-select", Select)
        size = self.query_one("#size-select", Select)
        quality = self.query_one("#quality-select", Select)
        output_dir = self.query_one("#output-dir-input", Input)
        button = self.query_one("#generate-button", Button)
        spinner = self.query_one("#run-spinner", LoadingIndicator)

        prompt.disabled = running
        prompt.read_only = running
        action.disabled = running
        format_select.disabled = running
        background.disabled = running
        size.disabled = running
        quality.disabled = running
        output_dir.disabled = running
        button.disabled = running
        button.label = "生成中…" if running else "生成"
        spinner.set_class(not running, "hidden")
        self._sync_compression_input()
        self._update_reference_action_state()

    def _sync_layout_mode(self) -> None:
        self.query_one("#main-layout").set_class(self.size.width < 110, "narrow")

    def _sync_compression_input(self) -> None:
        compression_input = self.query_one("#compression-input", Input)
        format_value = str(self.query_one("#format-select", Select).value)
        compression_enabled = not self._is_running and format_value in {"jpeg", "webp"}
        if format_value not in {"jpeg", "webp"}:
            compression_input.value = ""
        compression_input.disabled = not compression_enabled

    def _refresh_elapsed(self) -> None:
        if not self._is_running or self._started_at is None:
            return
        self._update_elapsed(time.perf_counter() - self._started_at)

    def _set_run_state(self, content: str) -> None:
        self.query_one("#run-state", Static).update(content)

    def _update_phase(self, phase: GenerationPhase) -> None:
        self.query_one("#phase-value", Static).update(phase.label)
        if self._is_running:
            self._update_summary(PHASE_DESCRIPTIONS[phase])

    def _update_elapsed(self, elapsed_seconds: float) -> None:
        self.query_one("#elapsed-value", Static).update(f"{elapsed_seconds:.1f}s")

    def _update_paths(self, copied_to: object | None, original_file: object | None) -> None:
        copied_value = str(copied_to) if copied_to else "尚未生成"
        origin_value = str(original_file) if original_file else "尚未发现"
        self.query_one("#copied-to-value", Static).update(copied_value)
        self.query_one("#origin-value", Static).update(origin_value)

    def _update_summary(self, summary: str) -> None:
        self.query_one("#summary-value", Static).update(summary)

    def _update_running_message(self, summary: str) -> None:
        if self._is_running:
            self._update_summary(summary)

    def _append_log(self, stream_name: str, chunk: str) -> None:
        if stream_name == "stdout":
            self._stdout_buffer = self._append_capped_text(self._stdout_buffer, chunk)
        else:
            self._stderr_buffer = self._append_capped_text(self._stderr_buffer, chunk)
        self._update_logs(
            self._stdout_buffer or "（尚未收到 stdout 输出，命令仍在运行）",
            self._stderr_buffer or "（尚未收到 stderr 输出）",
        )

    @staticmethod
    def _append_capped_text(existing: str, chunk: str, max_chars: int = 12000) -> str:
        merged = existing + chunk
        if len(merged) <= max_chars:
            return merged
        return "…\n" + merged[-(max_chars - 2) :]

    def _update_logs(self, stdout: str, stderr: str) -> None:
        self.query_one("#stdout-value", Static).update(truncate_block(stdout))
        self.query_one("#stderr-value", Static).update(truncate_block(stderr))

    def _refresh_reference_widgets(self, *, selected_id: str | None = None) -> None:
        reference_list = self.query_one("#reference-list", OptionList)
        current_selected_id = selected_id or self._get_selected_reference_id()

        reference_list.clear_options()
        for index, reference_image in enumerate(self._reference_images, start=1):
            suffix = " [主图]" if reference_image.id == self._primary_reference_image_id else ""
            reference_list.add_option(
                Option(f"{index}. {reference_image.path.name}{suffix}", id=reference_image.id)
            )

        if not self._reference_images:
            reference_list.highlighted = None
        else:
            target_id = current_selected_id
            if target_id is None or target_id not in {image.id for image in self._reference_images}:
                target_id = self._reference_images[0].id
            reference_list.highlighted = next(
                index for index, image in enumerate(self._reference_images) if image.id == target_id
            )

        self._update_reference_action_state()
        self._update_reference_summary()

    def _update_reference_action_state(self) -> None:
        reference_list = self.query_one("#reference-list", OptionList)
        paste_button = self.query_one("#paste-reference-button", Button)
        set_primary_button = self.query_one("#set-primary-button", Button)
        remove_button = self.query_one("#remove-reference-button", Button)
        clear_button = self.query_one("#clear-references-button", Button)
        selected_id = self._get_selected_reference_id()
        has_references = bool(self._reference_images)

        reference_list.disabled = self._is_running or not has_references
        paste_button.disabled = self._is_running
        set_primary_button.disabled = (
            self._is_running
            or not has_references
            or selected_id is None
            or selected_id == self._primary_reference_image_id
        )
        remove_button.disabled = self._is_running or not has_references or selected_id is None
        clear_button.disabled = self._is_running or not has_references

    def _update_reference_feedback(self, message: str) -> None:
        self.query_one("#reference-feedback", Static).update(message)

    def _update_reference_summary(self) -> None:
        self.query_one("#reference-summary-value", Static).update(
            summarize_reference_images(self._reference_images, self._primary_reference_image_id)
        )

    def _get_selected_reference_id(self) -> str | None:
        if not self.is_mounted:
            return None
        reference_list = self.query_one("#reference-list", OptionList)
        highlighted = reference_list.highlighted
        if highlighted is None or highlighted < 0 or highlighted >= len(self._reference_images):
            return None
        return self._reference_images[highlighted].id


def main() -> None:
    CodexCanvasApp().run()
