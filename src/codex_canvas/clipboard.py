from __future__ import annotations

import importlib
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .models import ReferenceImage


class ClipboardImageError(RuntimeError):
    """Raised when the system clipboard image cannot be extracted."""


class ClipboardBackendUnavailable(ClipboardImageError):
    """Raised when a clipboard backend is unavailable and the caller should try fallback."""


@dataclass(slots=True, frozen=True)
class ClipboardCommand:
    argv: tuple[str, ...]
    writes_to_stdout: bool


def create_session_temp_dir() -> Path:
    return Path(tempfile.mkdtemp(prefix="codexcanvas-session-"))


def paste_linux_clipboard_image_via_native_api(
    destination: Path,
    *,
    import_module: Callable[[str], object] = importlib.import_module,
) -> None:
    last_error: ClipboardBackendUnavailable | None = None
    for reader in (
        paste_linux_clipboard_image_via_gtk4,
        paste_linux_clipboard_image_via_gtk3,
    ):
        try:
            reader(destination, import_module=import_module)
            return
        except ClipboardBackendUnavailable as exc:
            last_error = exc

    raise last_error or ClipboardBackendUnavailable("Linux 原生剪贴板后端不可用。")


def paste_linux_clipboard_image_via_gtk4(
    destination: Path,
    *,
    import_module: Callable[[str], object] = importlib.import_module,
) -> None:
    try:
        gi = import_module("gi")
        gi.require_version("Gdk", "4.0")
        Gdk = import_module("gi.repository.Gdk")
        GLib = import_module("gi.repository.GLib")
    except (ImportError, ValueError, AttributeError) as exc:
        raise ClipboardBackendUnavailable("GTK4 剪贴板后端不可用。") from exc

    display = Gdk.Display.get_default()
    if display is None:
        raise ClipboardBackendUnavailable("GTK4 未拿到默认显示。")

    clipboard = display.get_clipboard()
    outcome: dict[str, object | None] = {
        "texture": None,
        "error": None,
    }
    loop = GLib.MainLoop()

    def on_read(current_clipboard: object, result: object) -> None:
        try:
            outcome["texture"] = current_clipboard.read_texture_finish(result)
        except Exception as exc:  # noqa: BLE001
            outcome["error"] = exc
        finally:
            loop.quit()

    try:
        clipboard.read_texture_async(None, on_read)
        loop.run()
    except Exception as exc:  # noqa: BLE001
        raise ClipboardBackendUnavailable("GTK4 剪贴板读取失败。") from exc

    if outcome["error"] is not None:
        raise ClipboardBackendUnavailable("GTK4 剪贴板读取失败。") from outcome["error"]

    texture = outcome["texture"]
    if texture is None:
        raise ClipboardImageError("剪贴板中没有可读取的图片。")
    if not texture.save_to_png(str(destination)):
        raise ClipboardImageError("剪贴板图片保存失败。")


def paste_linux_clipboard_image_via_gtk3(
    destination: Path,
    *,
    import_module: Callable[[str], object] = importlib.import_module,
) -> None:
    try:
        gi = import_module("gi")
        gi.require_version("Gtk", "3.0")
        Gtk = import_module("gi.repository.Gtk")
        Gdk = import_module("gi.repository.Gdk")
    except (ImportError, ValueError, AttributeError) as exc:
        raise ClipboardBackendUnavailable("GTK3 剪贴板后端不可用。") from exc

    display = Gdk.Display.get_default()
    if display is None:
        raise ClipboardBackendUnavailable("GTK3 未拿到默认显示。")

    clipboard = Gtk.Clipboard.get_default(display)
    if clipboard is None:
        raise ClipboardBackendUnavailable("GTK3 未拿到默认剪贴板。")

    try:
        pixbuf = clipboard.wait_for_image()
    except Exception as exc:  # noqa: BLE001
        raise ClipboardBackendUnavailable("GTK3 剪贴板读取失败。") from exc

    if pixbuf is None:
        raise ClipboardImageError("剪贴板中没有可读取的图片。")
    if not pixbuf.savev(str(destination), "png", [], []):
        raise ClipboardImageError("剪贴板图片保存失败。")


def choose_clipboard_image_command(
    *,
    platform: str = sys.platform,
    env: Mapping[str, str] | None = None,
    which: Callable[[str], str | None] = shutil.which,
) -> ClipboardCommand:
    current_env = os.environ if env is None else env

    if platform == "darwin":
        pngpaste = which("pngpaste")
        if pngpaste:
            return ClipboardCommand((pngpaste,), writes_to_stdout=False)
        raise ClipboardImageError("缺少剪贴板图片依赖：macOS 需要安装 pngpaste。")

    if platform.startswith("linux"):
        wl_paste = which("wl-paste")
        xclip = which("xclip")

        if current_env.get("WAYLAND_DISPLAY") and wl_paste:
            return ClipboardCommand(
                (wl_paste, "--no-newline", "--type", "image/png"),
                writes_to_stdout=True,
            )
        if current_env.get("DISPLAY") and xclip:
            return ClipboardCommand(
                (xclip, "-selection", "clipboard", "-t", "image/png", "-o"),
                writes_to_stdout=True,
            )
        if wl_paste:
            return ClipboardCommand(
                (wl_paste, "--no-newline", "--type", "image/png"),
                writes_to_stdout=True,
            )
        if xclip:
            return ClipboardCommand(
                (xclip, "-selection", "clipboard", "-t", "image/png", "-o"),
                writes_to_stdout=True,
            )
        raise ClipboardImageError(
            "缺少剪贴板图片依赖：Linux Wayland 需要 wl-paste，X11 需要 xclip。"
        )

    raise ClipboardImageError("当前平台暂不支持系统剪贴板图片读取。")


def paste_clipboard_image(
    session_dir: Path,
    *,
    chooser: Callable[..., ClipboardCommand] = choose_clipboard_image_command,
    subprocess_run: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run,
    import_module: Callable[[str], object] = importlib.import_module,
    now_factory: Callable[[], datetime] = datetime.now,
    uuid_factory: Callable[[], uuid.UUID] = uuid.uuid4,
    platform: str = sys.platform,
    env: Mapping[str, str] | None = None,
    which: Callable[[str], str | None] = shutil.which,
) -> ReferenceImage:
    session_dir.mkdir(parents=True, exist_ok=True)

    created_at = now_factory()
    image_uuid = uuid_factory()
    image_id = getattr(image_uuid, "hex", str(image_uuid))
    destination = session_dir / (
        f"clipboard-{created_at.strftime('%Y%m%d-%H%M%S')}-{image_id[:8]}.png"
    )

    try:
        command: ClipboardCommand | None = None
        if platform.startswith("linux"):
            try:
                paste_linux_clipboard_image_via_native_api(
                    destination,
                    import_module=import_module,
                )
            except ClipboardBackendUnavailable:
                command = chooser(platform=platform, env=env, which=which)
        else:
            command = chooser(platform=platform, env=env, which=which)

        if command is not None:
            if command.writes_to_stdout:
                result = subprocess_run(
                    list(command.argv),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=False,
                )
                if result.returncode != 0 or not result.stdout:
                    raise ClipboardImageError("剪贴板中没有可读取的图片。")
                destination.write_bytes(result.stdout)
            else:
                result = subprocess_run(
                    [*command.argv, str(destination)],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=False,
                )
                if (
                    result.returncode != 0
                    or not destination.exists()
                    or destination.stat().st_size == 0
                ):
                    raise ClipboardImageError("剪贴板中没有可读取的图片。")
    except ClipboardImageError:
        destination.unlink(missing_ok=True)
        raise
    except OSError as exc:
        destination.unlink(missing_ok=True)
        raise ClipboardImageError(f"剪贴板图片读取失败：{exc}") from exc

    return ReferenceImage(
        id=image_id,
        path=destination,
        created_at=created_at,
    )
