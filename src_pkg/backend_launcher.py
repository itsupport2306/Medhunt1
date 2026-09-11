"""Windows launcher for the packaged Medhunt localhost service."""
from __future__ import annotations

import argparse
import ctypes
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from urllib.error import URLError
from urllib.request import urlopen


SERVICE_NAME = "radixsol-sourcing-assistant"
DEFAULT_PORT = 8091
MUTEX_NAME = "Local\\RadixsolSourcingAssistantBackend8091"
ERROR_ALREADY_EXISTS = 183
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_TERMINATE = 0x0001


def app_home() -> Path:
    configured = os.getenv("RADIXSOL_HOME", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    if not bool(getattr(sys, "frozen", False)):
        return Path(__file__).resolve().parents[1]
    local = os.getenv("LOCALAPPDATA", "").strip()
    base = Path(local) if local else Path.home() / "AppData" / "Local"
    return (base / "Radixsol").resolve()


def message(title: str, body: str, *, error: bool = False) -> None:
    flags = 0x10 if error else 0x40
    try:
        ctypes.windll.user32.MessageBoxW(None, body, title, flags)
    except Exception:
        pass


def health(port: int, timeout: float = 0.8) -> dict:
    try:
        with urlopen(f"http://127.0.0.1:{port}/health", timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
            return payload if isinstance(payload, dict) else {}
    except (OSError, URLError, ValueError, json.JSONDecodeError):
        return {}


def same_service_running(port: int, attempts: int = 1) -> bool:
    for attempt in range(max(1, attempts)):
        payload = health(port)
        if payload.get("status") == "ok" and payload.get("service") == SERVICE_NAME:
            return True
        if attempt + 1 < attempts:
            time.sleep(0.5)
    return False


def port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.4)
        return probe.connect_ex(("127.0.0.1", port)) == 0


def process_image(handle) -> str:
    size = ctypes.c_ulong(32768)
    buffer = ctypes.create_unicode_buffer(size.value)
    if ctypes.windll.kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
        return buffer.value
    return ""


def shutdown() -> int:
    pid_file = app_home() / "backend.pid"
    try:
        pid = int(pid_file.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return 0
    if pid == os.getpid():
        return 0

    handle = ctypes.windll.kernel32.OpenProcess(
        PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_TERMINATE, False, pid,
    )
    if not handle:
        pid_file.unlink(missing_ok=True)
        return 0
    try:
        running_image = process_image(handle)
        expected_image = str(Path(sys.executable).resolve())
        if not running_image or os.path.normcase(running_image) != os.path.normcase(expected_image):
            return 2
        if not ctypes.windll.kernel32.TerminateProcess(handle, 0):
            return 3
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)

    for _ in range(30):
        if not health(int(os.getenv("RADIXSOL_PORT", DEFAULT_PORT))):
            break
        time.sleep(0.1)
    pid_file.unlink(missing_ok=True)
    return 0


def configure_logging(log_path: Path) -> dict:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    if not any(isinstance(item, RotatingFileHandler) for item in root.handlers):
        handler = RotatingFileHandler(
            log_path, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
        root.addHandler(handler)
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "default": {"format": "%(asctime)s %(levelname)s %(name)s %(message)s"},
        },
        "handlers": {
            "file": {
                "class": "logging.handlers.RotatingFileHandler",
                "filename": str(log_path),
                "maxBytes": 2 * 1024 * 1024,
                "backupCount": 3,
                "encoding": "utf-8",
                "formatter": "default",
            },
        },
        "loggers": {
            "uvicorn": {"handlers": ["file"], "level": "INFO", "propagate": False},
            "uvicorn.error": {"handlers": ["file"], "level": "INFO", "propagate": False},
            "uvicorn.access": {"handlers": ["file"], "level": "WARNING", "propagate": False},
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--shutdown", action="store_true")
    parser.add_argument("--status", action="store_true")
    args, _ = parser.parse_known_args()

    home = app_home()
    if bool(getattr(sys, "frozen", False)):
        os.environ.setdefault("RADIXSOL_HOME", str(home))
    port = int(os.getenv("RADIXSOL_PORT", str(DEFAULT_PORT)))

    if args.shutdown:
        return shutdown()
    if args.status:
        return 0 if same_service_running(port) else 1

    for child in (home / "config", home / "data", home / "logs"):
        child.mkdir(parents=True, exist_ok=True)
    log_path = home / "logs" / "backend.log"
    log_config = configure_logging(log_path)
    logger = logging.getLogger("radixsol.launcher")

    mutex = ctypes.windll.kernel32.CreateMutexW(None, True, MUTEX_NAME)
    if not mutex:
        message("Medhunt", "The Medhunt backend could not acquire its startup lock.", error=True)
        return 4
    already_exists = ctypes.windll.kernel32.GetLastError() == ERROR_ALREADY_EXISTS
    if already_exists:
        ctypes.windll.kernel32.CloseHandle(mutex)
        if same_service_running(port, attempts=20):
            return 0
        message(
            "Medhunt",
            "Medhunt is already starting, but it did not become ready. Check the backend log in "
            f"{log_path}.",
            error=True,
        )
        return 5

    pid_file = home / "backend.pid"
    try:
        if same_service_running(port):
            return 0
        if port_in_use(port):
            message(
                "Medhunt port conflict",
                f"Port {port} is being used by another application. Close that application, then "
                "start Medhunt Backend again.",
                error=True,
            )
            logger.error("Port %s is occupied by a non-Medhunt service", port)
            return 6

        pid_file.write_text(str(os.getpid()), encoding="utf-8")
        logger.info("Starting Medhunt %s on 127.0.0.1:%s", "3.22.15", port)
        import uvicorn
        from api import app

        uvicorn.run(
            app,
            host="127.0.0.1",
            port=port,
            log_config=log_config,
            access_log=False,
        )
        return 0
    except Exception:
        logger.exception("Medhunt backend stopped unexpectedly")
        message(
            "Medhunt backend error",
            f"The local backend could not start. See {log_path} for details.",
            error=True,
        )
        return 7
    finally:
        try:
            if pid_file.exists() and pid_file.read_text(encoding="utf-8").strip() == str(os.getpid()):
                pid_file.unlink(missing_ok=True)
        except OSError:
            pass
        ctypes.windll.kernel32.ReleaseMutex(mutex)
        ctypes.windll.kernel32.CloseHandle(mutex)


if __name__ == "__main__":
    raise SystemExit(main())
