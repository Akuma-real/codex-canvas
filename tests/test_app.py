from __future__ import annotations

import asyncio
from datetime import datetime
from importlib import resources
from pathlib import Path

from textual.widgets import Input, OptionList, Select, Static, TextArea

from codex_canvas import __main__ as module_entry
from codex_canvas.app import (
    ACTION_OPTIONS,
    BACKGROUND_OPTIONS,
    FORMAT_OPTIONS,
    PASTE_REFERENCE_BINDING,
    QUALITY_OPTIONS,
    SIZE_OPTIONS,
    CodexCanvasApp,
    summarize_reference_images,
)
from codex_canvas.app import (
    main as app_main,
)
from codex_canvas.models import GenerationPhase, GenerationResult, ReferenceImage


def make_reference_image(
    session_dir: Path,
    image_id: str,
    filename: str,
) -> ReferenceImage:
    path = session_dir / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"png")
    return ReferenceImage(
        id=image_id,
        path=path,
        created_at=datetime(2026, 4, 22, 12, 0, 0),
    )


def test_packaged_stylesheet_is_embedded() -> None:
    css = resources.files("codex_canvas").joinpath("app.tcss").read_text(encoding="utf-8")

    assert css.strip()
    assert CodexCanvasApp.CSS.strip() == css.strip()


def test_summarize_reference_images_reports_primary(tmp_path: Path) -> None:
    first = make_reference_image(tmp_path, "ref-1", "first.png")
    second = make_reference_image(tmp_path, "ref-2", "second.png")

    summary = summarize_reference_images([first, second], "ref-2")

    assert "2 张参考图" in summary
    assert "second.png" in summary


def test_app_reference_actions_update_state(tmp_path: Path) -> None:
    session_dir = tmp_path / "session"
    generated: list[ReferenceImage] = []
    counter = {"value": 0}

    def fake_clipboard_paste(target_dir: Path) -> ReferenceImage:
        counter["value"] += 1
        reference_image = make_reference_image(
            target_dir,
            f"ref-{counter['value']}",
            f"clipboard-{counter['value']}.png",
        )
        generated.append(reference_image)
        return reference_image

    async def scenario() -> None:
        app = CodexCanvasApp(
            clipboard_paste=fake_clipboard_paste,
            session_dir_factory=lambda: session_dir,
        )

        async with app.run_test() as pilot:
            await pilot.pause()
            app._paste_reference_image_from_clipboard()
            await pilot.pause()

            assert app._primary_reference_image_id == "ref-1"
            assert app.query_one("#reference-list", OptionList).option_count == 1
            assert "clipboard-1.png" in app.query_one("#reference-summary-value", Static).content

            app._paste_reference_image_from_clipboard()
            await pilot.pause()

            reference_list = app.query_one("#reference-list", OptionList)
            reference_list.highlighted = 1
            await pilot.pause()
            app._set_selected_as_primary_reference()
            await pilot.pause()

            assert app._primary_reference_image_id == "ref-2"
            assert "clipboard-2.png" in app.query_one("#reference-summary-value", Static).content

            app._remove_selected_reference_image()
            await pilot.pause()

            assert app._primary_reference_image_id == "ref-1"
            assert reference_list.option_count == 1

            app._clear_reference_images()
            await pilot.pause()

            assert app._primary_reference_image_id is None
            assert app.query_one("#reference-list", OptionList).option_count == 0
            assert app.query_one("#reference-summary-value", Static).content == "当前无参考图。"

    asyncio.run(scenario())


def test_paste_reference_binding_includes_terminal_shortcuts() -> None:
    assert PASTE_REFERENCE_BINDING == "ctrl+v,ctrl+shift+v,shift+insert,super+v"


def test_app_advanced_selects_use_chinese_labels_with_english_values(tmp_path: Path) -> None:
    async def scenario() -> None:
        app = CodexCanvasApp(session_dir_factory=lambda: tmp_path / "session")

        async with app.run_test() as pilot:
            await pilot.pause()

            action = app.query_one("#action-select", Select)
            output_format = app.query_one("#format-select", Select)
            background = app.query_one("#background-select", Select)
            size = app.query_one("#size-select", Select)
            quality = app.query_one("#quality-select", Select)

            assert action._options == list(ACTION_OPTIONS)
            assert output_format._options == list(FORMAT_OPTIONS)
            assert background._options == list(BACKGROUND_OPTIONS)
            assert size._options == list(SIZE_OPTIONS)
            assert quality._options == list(QUALITY_OPTIONS)
            assert action.value == "auto"
            assert output_format.value == "png"
            assert background.value == "auto"
            assert size.value == "1024x1024"
            assert quality.value == "high"

    asyncio.run(scenario())


def test_app_paste_shortcut_triggers_clipboard_read(tmp_path: Path) -> None:
    session_dir = tmp_path / "session"

    def fake_clipboard_paste(target_dir: Path) -> ReferenceImage:
        return make_reference_image(target_dir, "ref-shortcut", "shortcut.png")

    async def scenario() -> None:
        app = CodexCanvasApp(
            clipboard_paste=fake_clipboard_paste,
            session_dir_factory=lambda: session_dir,
        )

        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("ctrl+v")
            await pilot.pause()

            assert app._primary_reference_image_id == "ref-shortcut"
            assert app.query_one("#reference-list", OptionList).option_count == 1
            assert "shortcut.png" in app.query_one("#reference-summary-value", Static).content

    asyncio.run(scenario())


def test_reference_action_grid_keeps_columns_aligned(tmp_path: Path) -> None:
    async def scenario() -> None:
        app = CodexCanvasApp(session_dir_factory=lambda: tmp_path / "session")

        async with app.run_test(size=(160, 50)) as pilot:
            await pilot.pause()

            paste = app.query_one("#paste-reference-button")
            remove = app.query_one("#remove-reference-button")
            set_primary = app.query_one("#set-primary-button")
            clear = app.query_one("#clear-references-button")

            assert paste.region.x == remove.region.x
            assert paste.region.width == remove.region.width
            assert set_primary.region.x == clear.region.x
            assert set_primary.region.width == clear.region.width
            assert paste.region.right < set_primary.region.x

    asyncio.run(scenario())


def test_app_generate_without_references_keeps_existing_flow(tmp_path: Path) -> None:
    requests = []

    def fake_runner(request, **_kwargs):  # type: ignore[no-untyped-def]
        requests.append(request)
        return GenerationResult(
            request=request,
            success=True,
            phase=GenerationPhase.PRESENTING_RESULT,
            summary="ok",
            elapsed_seconds=0.1,
            copied_to=tmp_path / "output.png",
            original_file=tmp_path / "original.png",
            command=("codex", "exec"),
            exit_code=0,
        )

    async def scenario() -> None:
        app = CodexCanvasApp(
            runner=fake_runner,
            session_dir_factory=lambda: tmp_path / "session",
        )

        async with app.run_test() as pilot:
            await pilot.pause()

            prompt = app.query_one("#prompt-input", TextArea)
            prompt.text = "clean product shot"
            app.query_one("#format-select", Select).value = "jpeg"
            await pilot.pause()

            compression = app.query_one("#compression-input", Input)
            assert compression.disabled is False
            compression.value = "75"

            app.query_one("#size-select", Select).value = "auto"
            app.query_one("#quality-select", Select).value = "auto"
            app.run_worker = lambda work, **_kwargs: (  # type: ignore[method-assign]
                app._finish_run(work()),
                None,
            )[1]
            app.action_generate()
            await pilot.pause()

            assert len(requests) == 1
            assert requests[0].prompt == "clean product shot"
            assert requests[0].reference_images == ()
            assert requests[0].primary_reference_image_id is None
            assert requests[0].output_format == "jpeg"
            assert requests[0].compression == 75
            assert requests[0].size == "auto"
            assert requests[0].quality == "auto"
            assert app.query_one("#run-state", Static).content == "生成成功"

    asyncio.run(scenario())


def test_app_main_runs_tui(monkeypatch) -> None:
    called = {"ran": False}

    def fake_run(self) -> None:
        called["ran"] = True

    monkeypatch.setattr(CodexCanvasApp, "run", fake_run)

    app_main()

    assert called["ran"] is True


def test_module_entry_runs_tui(monkeypatch) -> None:
    called = {"ran": False}

    def fake_run_app() -> None:
        called["ran"] = True

    monkeypatch.setattr(module_entry, "run_app", fake_run_app)

    module_entry.main()

    assert called["ran"] is True
