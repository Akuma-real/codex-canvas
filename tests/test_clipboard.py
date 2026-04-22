from __future__ import annotations

import subprocess
import uuid
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from codex_canvas.clipboard import (
    ClipboardCommand,
    ClipboardImageError,
    choose_clipboard_image_command,
    paste_clipboard_image,
)


def build_importer(modules: dict[str, object]) -> object:
    def import_module(name: str) -> object:
        if name not in modules:
            raise ImportError(name)
        return modules[name]

    return import_module


def test_choose_clipboard_image_command_uses_pngpaste_on_macos() -> None:
    command = choose_clipboard_image_command(
        platform="darwin",
        env={},
        which=lambda name: f"/usr/bin/{name}" if name == "pngpaste" else None,
    )

    assert command == ClipboardCommand(("/usr/bin/pngpaste",), writes_to_stdout=False)


def test_choose_clipboard_image_command_uses_wl_paste_on_wayland() -> None:
    command = choose_clipboard_image_command(
        platform="linux",
        env={"WAYLAND_DISPLAY": "wayland-1"},
        which=lambda name: f"/usr/bin/{name}" if name == "wl-paste" else None,
    )

    assert command == ClipboardCommand(
        ("/usr/bin/wl-paste", "--no-newline", "--type", "image/png"),
        writes_to_stdout=True,
    )


def test_choose_clipboard_image_command_uses_xclip_on_x11() -> None:
    command = choose_clipboard_image_command(
        platform="linux",
        env={"DISPLAY": ":0"},
        which=lambda name: f"/usr/bin/{name}" if name == "xclip" else None,
    )

    assert command == ClipboardCommand(
        ("/usr/bin/xclip", "-selection", "clipboard", "-t", "image/png", "-o"),
        writes_to_stdout=True,
    )


def test_choose_clipboard_image_command_raises_when_dependency_missing() -> None:
    with pytest.raises(ClipboardImageError, match="缺少剪贴板图片依赖"):
        choose_clipboard_image_command(
            platform="linux",
            env={},
            which=lambda _name: None,
        )


def test_paste_clipboard_image_raises_when_clipboard_has_no_image(tmp_path: Path) -> None:
    def fake_run(*_args, **_kwargs) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(
            args=["wl-paste"],
            returncode=1,
            stdout=b"",
            stderr=b"no image",
        )

    with pytest.raises(ClipboardImageError, match="剪贴板中没有可读取的图片"):
        paste_clipboard_image(
            tmp_path,
            chooser=lambda **_kwargs: ClipboardCommand(("wl-paste",), writes_to_stdout=True),
            subprocess_run=fake_run,
        )


def test_paste_clipboard_image_uses_gtk4_native_backend_before_shell_fallback(
    tmp_path: Path,
) -> None:
    destination_bytes = b"gtk4-png"

    class FakeTexture:
        def save_to_png(self, path: str) -> bool:
            Path(path).write_bytes(destination_bytes)
            return True

    class FakeClipboard:
        def read_texture_async(self, _cancellable, callback) -> None:  # type: ignore[no-untyped-def]
            callback(self, object())

        def read_texture_finish(self, _result: object) -> FakeTexture:
            return FakeTexture()

    class FakeDisplay:
        @staticmethod
        def get_default() -> object:
            return SimpleNamespace(get_clipboard=lambda: FakeClipboard())

    class FakeLoop:
        def run(self) -> None:
            return None

        def quit(self) -> None:
            return None

    reference_image = paste_clipboard_image(
        tmp_path,
        chooser=lambda **_kwargs: (_ for _ in ()).throw(AssertionError("不应触发命令回退")),
        import_module=build_importer(
            {
                "gi": SimpleNamespace(require_version=lambda *_args: None),
                "gi.repository.Gdk": SimpleNamespace(Display=FakeDisplay),
                "gi.repository.GLib": SimpleNamespace(MainLoop=FakeLoop),
            }
        ),
    )

    assert reference_image.path.read_bytes() == destination_bytes


def test_paste_clipboard_image_falls_back_to_shell_when_native_backend_unavailable(
    tmp_path: Path,
) -> None:
    png_bytes = b"\x89PNG\r\n\x1a\nfallback"

    def fake_run(*_args, **_kwargs) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(
            args=["wl-paste"],
            returncode=0,
            stdout=png_bytes,
            stderr=b"",
        )

    reference_image = paste_clipboard_image(
        tmp_path,
        chooser=lambda **_kwargs: ClipboardCommand(("wl-paste",), writes_to_stdout=True),
        subprocess_run=fake_run,
        import_module=build_importer({}),
    )

    assert reference_image.path.read_bytes() == png_bytes


def test_paste_clipboard_image_uses_gtk3_when_gtk4_is_unavailable(tmp_path: Path) -> None:
    destination_bytes = b"gtk3-png"

    class FakePixbuf:
        def savev(
            self,
            path: str,
            _image_type: str,
            _option_keys: list[str],
            _option_values: list[str],
        ) -> bool:
            Path(path).write_bytes(destination_bytes)
            return True

    class FakeClipboard:
        def wait_for_image(self) -> FakePixbuf:
            return FakePixbuf()

    class FakeGtkClipboard:
        @staticmethod
        def get_default(_display: object) -> FakeClipboard:
            return FakeClipboard()

    class FakeDisplay:
        @staticmethod
        def get_default() -> object:
            return object()

    reference_image = paste_clipboard_image(
        tmp_path,
        chooser=lambda **_kwargs: (_ for _ in ()).throw(AssertionError("不应触发命令回退")),
        import_module=build_importer(
            {
                "gi": SimpleNamespace(
                    require_version=lambda namespace, version: (
                        (_ for _ in ()).throw(ValueError("gtk4 unavailable"))
                        if (namespace, version) == ("Gdk", "4.0")
                        else None
                    )
                ),
                "gi.repository.Gtk": SimpleNamespace(Clipboard=FakeGtkClipboard),
                "gi.repository.Gdk": SimpleNamespace(Display=FakeDisplay),
            }
        ),
    )

    assert reference_image.path.read_bytes() == destination_bytes


def test_paste_clipboard_image_writes_temp_png_and_returns_reference_image(tmp_path: Path) -> None:
    png_bytes = b"\x89PNG\r\n\x1a\npayload"
    created_at = datetime(2026, 4, 22, 12, 34, 56)
    fixed_uuid = uuid.UUID("12345678-1234-5678-1234-567812345678")

    def fake_run(*_args, **_kwargs) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(
            args=["wl-paste"],
            returncode=0,
            stdout=png_bytes,
            stderr=b"",
        )

    reference_image = paste_clipboard_image(
        tmp_path,
        chooser=lambda **_kwargs: ClipboardCommand(("wl-paste",), writes_to_stdout=True),
        subprocess_run=fake_run,
        now_factory=lambda: created_at,
        uuid_factory=lambda: fixed_uuid,
    )

    assert reference_image.id == fixed_uuid.hex
    assert reference_image.source == "clipboard"
    assert reference_image.created_at == created_at
    assert reference_image.path.parent == tmp_path
    assert reference_image.path.suffix == ".png"
    assert reference_image.path.read_bytes() == png_bytes
