"""Per-user installer for the standalone Medhunt Watcher extension."""
from __future__ import annotations

import ctypes
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

from dotenv import dotenv_values


APP_NAME = "Medhunt Watcher Setup"
MB_OK = 0
MB_ICONERROR = 0x10
MB_ICONINFORMATION = 0x40


def local_app_data() -> Path:
    raw = os.getenv("LOCALAPPDATA", "").strip()
    return (Path(raw) if raw else Path.home() / "AppData" / "Local").resolve()


def install_root() -> Path:
    return local_app_data() / "Programs" / "Radixsol"


def data_root() -> Path:
    return local_app_data() / "Radixsol"


def payload_root() -> Path:
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent)) / "payload"


def message(body: str, *, flags: int = MB_OK | MB_ICONINFORMATION) -> None:
    ctypes.windll.user32.MessageBoxW(None, body, APP_NAME, flags)


def valid_token(value: str) -> bool:
    return len(value) >= 32 and bool(re.fullmatch(r"[A-Za-z0-9_-]+", value))


def installed_token() -> str:
    config_dir = data_root() / "config"
    # The normal installer deliberately keeps this per-machine secret in the
    # upgrade-preserved override. Older development builds may have placed it
    # in the baseline, so retain that read-only fallback.
    for config in (config_dir / ".env.local", config_dir / ".env"):
        if not config.is_file():
            continue
        token = str(dotenv_values(config).get("MEDHUNT_LOCAL_API_TOKEN") or "").strip()
        if valid_token(token):
            return token
    raise RuntimeError(
        "The installed Medhunt service does not have a valid local connection key. "
        "Run the normal Medhunt Setup once, then run Watcher Setup again."
    )


def inject_token(extension: Path, token: str) -> None:
    script = extension / "watcher-background.js"
    body = script.read_text(encoding="utf-8")
    marker = "__MEDHUNT_LOCAL_API_TOKEN__"
    if marker not in body:
        raise RuntimeError("The watcher package is missing its connection-key marker.")
    temporary = script.with_suffix(".js.tmp")
    temporary.write_text(body.replace(marker, token), encoding="utf-8", newline="\n")
    os.replace(temporary, script)


def remove_exact(path: Path, expected: Path) -> None:
    if not path.exists():
        return
    if os.path.normcase(str(path.resolve())) != os.path.normcase(str(expected.resolve())):
        raise RuntimeError(f"Refusing to replace an unexpected directory: {path}")
    shutil.rmtree(path)


def install() -> int:
    source = payload_root()
    if not (source / "watcher" / "manifest.json").is_file():
        message("This setup package is incomplete.", flags=MB_OK | MB_ICONERROR)
        return 2
    try:
        token = installed_token()
        root = install_root()
        target = root / "watcher-extension"
        stage = root.parent / f".Medhunt-Watcher-installing-{os.getpid()}"
        remove_exact(stage, root.parent / stage.name)
        stage.mkdir(parents=True)
        try:
            shutil.copytree(source / "watcher", stage / "watcher-extension")
            inject_token(stage / "watcher-extension", token)
            guide = (source / "Install Watcher.html").read_text(encoding="utf-8")
            guide = guide.replace("{{WATCHER_PATH}}", str(target))
            (stage / "Install Watcher.html").write_text(guide, encoding="utf-8", newline="\n")
            root.mkdir(parents=True, exist_ok=True)
            remove_exact(target, root / "watcher-extension")
            shutil.move(str(stage / "watcher-extension"), str(target))
            guide_target = root / "Install Watcher.html"
            shutil.copy2(stage / "Install Watcher.html", guide_target)
        finally:
            remove_exact(stage, root.parent / stage.name)
        message(
            "Medhunt Watcher is ready.\n\n"
            "The browser installation guide will open next. Use its one-time Load unpacked step."
        )
        try:
            os.startfile(root / "Install Watcher.html")
            subprocess.Popen(["explorer.exe", str(target)])
        except OSError:
            pass
        return 0
    except Exception as exc:
        message(str(exc), flags=MB_OK | MB_ICONERROR)
        return 2


if __name__ == "__main__":
    raise SystemExit(install())
