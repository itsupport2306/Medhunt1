"""Per-user Windows installer for the Medhunt backend and unpacked extension."""
from __future__ import annotations

import ctypes
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from urllib.request import urlopen
import winreg

from dotenv import dotenv_values


APP_NAME = "Medhunt"
APP_VERSION = "3.23.0"
SERVICE_NAME = "radixsol-sourcing-assistant"
UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\RadixsolSourcingAssistant"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "Medhunt"
LEGACY_APP_NAME = "Radixsol Sourcing Assistant"
LEGACY_RUN_VALUE = "Radixsol Sourcing Assistant"
MB_OK = 0x00000000
MB_YESNO = 0x00000004
MB_YESNOCANCEL = 0x00000003
MB_ICONINFORMATION = 0x00000040
MB_ICONWARNING = 0x00000030
MB_ICONERROR = 0x00000010
IDYES = 6
IDNO = 7
IDCANCEL = 2
CREATE_NO_WINDOW = 0x08000000
DETACHED_PROCESS = 0x00000008
MOVEFILE_DELAY_UNTIL_REBOOT = 0x00000004

CENTRAL_STORAGE_OVERRIDE_KEYS = frozenset({
    "DATABASE_BACKEND",
    "DATABASE_NAME",
    "DATABASE_URL",
    "STORAGE_ENABLED",
    "S3_ENDPOINT_URL",
    "S3_ACCESS_KEY",
    "S3_SECRET_KEY",
    "S3_BUCKET",
    "S3_REGION",
    "S3_PUBLIC_BASE_URL",
})


def local_app_data() -> Path:
    raw = os.getenv("LOCALAPPDATA", "").strip()
    return (Path(raw) if raw else Path.home() / "AppData" / "Local").resolve()


def install_root() -> Path:
    return local_app_data() / "Programs" / "Radixsol"


def data_root() -> Path:
    return local_app_data() / "Radixsol"


def payload_root() -> Path:
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent)) / "payload"


def message(body: str, *, title: str = APP_NAME, flags: int = MB_OK | MB_ICONINFORMATION) -> int:
    return int(ctypes.windll.user32.MessageBoxW(None, body, title, flags))


def validate_exact(path: Path, expected: Path) -> Path:
    resolved = path.resolve()
    if os.path.normcase(str(resolved)) != os.path.normcase(str(expected.resolve())):
        raise RuntimeError(f"Refusing to modify unexpected path: {resolved}")
    return resolved


def remove_tree(path: Path, expected: Path) -> None:
    target = validate_exact(path, expected)
    if target.exists():
        shutil.rmtree(target)


def stop_backend(root: Path) -> None:
    executable = root / "backend" / "RadixsolBackend.exe"
    if not executable.exists():
        return
    try:
        subprocess.run(
            [str(executable), "--shutdown"],
            cwd=str(executable.parent),
            timeout=12,
            check=False,
            creationflags=CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass
    # A PyInstaller onedir application has a bootloader parent and a Python
    # child.  The graceful command stops the PID recorded by the child, but on
    # some Windows builds the parent can remain alive briefly and keep the old
    # executable/DLLs locked.  Terminate only Medhunt's uniquely named process
    # tree before replacing its installation directory.
    try:
        subprocess.run(
            ["taskkill.exe", "/F", "/T", "/IM", "RadixsolBackend.exe"],
            timeout=10,
            check=False,
            creationflags=CREATE_NO_WINDOW,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass
    time.sleep(0.5)


def powershell_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def create_shortcut(link: Path, target: Path, description: str, arguments: str = "") -> None:
    link.parent.mkdir(parents=True, exist_ok=True)
    script = (
        "$w=New-Object -ComObject WScript.Shell;"
        f"$s=$w.CreateShortcut({powershell_quote(str(link))});"
        f"$s.TargetPath={powershell_quote(str(target))};"
        f"$s.WorkingDirectory={powershell_quote(str(target.parent))};"
        f"$s.Description={powershell_quote(description)};"
    )
    if arguments:
        script += f"$s.Arguments={powershell_quote(arguments)};"
    script += "$s.Save()"
    subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        check=False,
        timeout=20,
        creationflags=CREATE_NO_WINDOW,
    )


def start_menu_dir() -> Path:
    app_data = Path(os.getenv("APPDATA", str(Path.home() / "AppData" / "Roaming")))
    return app_data / "Microsoft" / "Windows" / "Start Menu" / "Programs" / APP_NAME


def legacy_start_menu_dir() -> Path:
    app_data = Path(os.getenv("APPDATA", str(Path.home() / "AppData" / "Roaming")))
    return app_data / "Microsoft" / "Windows" / "Start Menu" / "Programs" / LEGACY_APP_NAME


def write_default_config() -> Path:
    config_dir = data_root() / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    destination = config_dir / ".env.local"
    if not destination.exists():
        destination.write_text(
            "# Managed by Medhunt Setup.\n"
            "# This file is preserved when Medhunt is upgraded.\n",
            encoding="utf-8",
        )
    return destination


def _valid_local_api_token(value: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9_-]{40,128}", str(value or "")))


def _atomic_text_replace(destination: Path, body: str) -> None:
    handle, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent, text=True,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(body)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def enforce_central_storage_config(baseline: Path) -> Path:
    """Keep packaged Neon/R2 routing authoritative across upgrades.

    Older Medhunt builds created an administrator override that deliberately
    selected SQLite and disabled R2.  Setup preserves ``.env.local`` because it
    also contains the per-install browser token, so those stale assignments
    would otherwise keep defeating the refreshed trusted-team baseline.

    Only central-storage routing keys are removed.  The machine token and
    unrelated administrator settings remain untouched.
    """
    values = dotenv_values(baseline)
    cloud_ready = bool(
        str(values.get("DATABASE_BACKEND") or "").strip().casefold() != "sqlite"
        and str(values.get("DATABASE_URL") or "").strip()
        and str(values.get("STORAGE_ENABLED") or "").strip().casefold()
        in {"1", "true", "yes"}
        and str(values.get("S3_ENDPOINT_URL") or "").strip()
        and str(values.get("S3_ACCESS_KEY") or "").strip()
        and str(values.get("S3_SECRET_KEY") or "").strip()
        and str(values.get("S3_BUCKET") or "").strip()
    )
    if not cloud_ready:
        raise RuntimeError("The packaged Neon/R2 configuration is incomplete.")

    destination = write_default_config()
    body = destination.read_text(encoding="utf-8")
    assignment = re.compile(
        r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=",
        re.IGNORECASE,
    )
    retained = []
    changed = False
    for line in body.splitlines():
        match = assignment.match(line)
        if match and match.group(1).upper() in CENTRAL_STORAGE_OVERRIDE_KEYS:
            changed = True
            continue
        retained.append(line)
    if changed:
        while retained and not retained[-1].strip():
            retained.pop()
        _atomic_text_replace(destination, "\n".join([*retained, ""]))
    return destination


def ensure_local_api_token() -> str:
    """Return one canonical, strong per-install token using dotenv semantics."""
    destination = write_default_config()
    body = destination.read_text(encoding="utf-8")
    assignment = re.compile(
        r"^\s*(?:export\s+)?MEDHUNT_LOCAL_API_TOKEN\s*=", re.IGNORECASE,
    )
    assignments = [line for line in body.splitlines() if assignment.match(line)]
    configured = str(
        dotenv_values(destination).get("MEDHUNT_LOCAL_API_TOKEN") or ""
    ).strip()
    if len(assignments) == 1 and _valid_local_api_token(configured):
        return configured

    token = secrets.token_urlsafe(32)
    retained = [line for line in body.splitlines() if not assignment.match(line)]
    while retained and not retained[-1].strip():
        retained.pop()
    normalized = "\n".join([
        *retained,
        *( [""] if retained else [] ),
        f"MEDHUNT_LOCAL_API_TOKEN='{token}'",
        "",
    ])
    _atomic_text_replace(destination, normalized)
    return token


def inject_extension_token(extension_dir: Path, token: str) -> None:
    if not _valid_local_api_token(token):
        raise RuntimeError("The local API token is invalid.")
    script = extension_dir / "app.js"
    body = script.read_text(encoding="utf-8")
    marker = "__MEDHUNT_LOCAL_API_TOKEN__"
    if marker not in body:
        raise RuntimeError("The extension is missing its local authentication marker.")
    script.write_text(body.replace(marker, token), encoding="utf-8", newline="\n")


def install_bundled_lookup_config(source: Path) -> Path:
    """Atomically install the trusted-team lookup baseline.

    ``.env.local`` remains separate and is preserved as the administrator
    override, while this baseline is refreshed on every application upgrade so
    packaged credentials can be rotated.
    """
    if not source.is_file():
        raise RuntimeError("The setup package is missing its lookup configuration.")
    config_dir = data_root() / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    destination = config_dir / ".env"
    handle, temporary_name = tempfile.mkstemp(
        prefix=".env.", suffix=".tmp", dir=config_dir
    )
    os.close(handle)
    temporary = Path(temporary_name)
    try:
        shutil.copyfile(source, temporary)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def register_install(root: Path, setup_copy: Path) -> None:
    backend = root / "backend" / "RadixsolBackend.exe"
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
        if LEGACY_RUN_VALUE != RUN_VALUE:
            try:
                winreg.DeleteValue(key, LEGACY_RUN_VALUE)
            except FileNotFoundError:
                pass
        winreg.SetValueEx(key, RUN_VALUE, 0, winreg.REG_SZ, f'"{backend}"')

    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY) as key:
        values = {
            "DisplayName": APP_NAME,
            "DisplayVersion": APP_VERSION,
            "Publisher": "Radixsol",
            "InstallLocation": str(root),
            "DisplayIcon": str(backend),
            "UninstallString": f'"{setup_copy}" --uninstall',
            "QuietUninstallString": f'"{setup_copy}" --uninstall --silent',
        }
        for name, value in values.items():
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)
        winreg.SetValueEx(key, "NoModify", 0, winreg.REG_DWORD, 1)
        winreg.SetValueEx(key, "NoRepair", 0, winreg.REG_DWORD, 1)


def unregister_install() -> None:
    for value_name in {RUN_VALUE, LEGACY_RUN_VALUE}:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
                winreg.DeleteValue(key, value_name)
        except FileNotFoundError:
            pass
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY)
    except FileNotFoundError:
        pass


def render_guide(template: Path, destination: Path, extension_dir: Path) -> None:
    html = template.read_text(encoding="utf-8")
    html = html.replace("{{EXTENSION_PATH}}", str(extension_dir))
    html = html.replace("{{DATA_PATH}}", str(data_root()))
    destination.write_text(html, encoding="utf-8")


def backend_ready(timeout: float = 20.0) -> bool:
    port = int(os.getenv("RADIXSOL_PORT", "8091"))
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urlopen(f"http://127.0.0.1:{port}/health", timeout=0.8) as response:
                result = json.loads(response.read().decode("utf-8"))
                if result.get("service") == SERVICE_NAME and result.get("status") == "ok":
                    return True
        except (OSError, ValueError):
            pass
        time.sleep(0.4)
    return False


def launch_backend(root: Path) -> bool:
    executable = root / "backend" / "RadixsolBackend.exe"
    environment = os.environ.copy()
    # The setup and backend are independent PyInstaller applications. Without
    # this reset, the backend can inherit the setup bootloader's extraction
    # directory and keep the one-file setup process (and release EXE) locked
    # for the entire lifetime of the service.
    environment["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    subprocess.Popen(
        [str(executable)],
        cwd=str(executable.parent),
        close_fds=True,
        creationflags=CREATE_NO_WINDOW | DETACHED_PROCESS,
        env=environment,
    )
    return backend_ready()


def install(*, silent: bool = False) -> int:
    source = payload_root()
    required = [
        source / "backend" / "RadixsolBackend.exe",
        source / "extension" / "manifest.json",
        source / "lookup-config.env",
    ]
    if not all(item.exists() for item in required):
        message("This setup package is incomplete and cannot be installed.", flags=MB_OK | MB_ICONERROR)
        return 2

    root = validate_exact(install_root(), local_app_data() / "Programs" / "Radixsol")
    if not silent:
        choice = message(
            "Install or update Medhunt for this Windows user?\n\n"
            "The local backend will start automatically at sign-in. Chrome/Edge still requires "
            "one manual Load unpacked step.",
            flags=MB_YESNO | MB_ICONINFORMATION,
        )
        if choice != IDYES:
            return 1

    stage = root.parent / f".Radixsol-installing-{os.getpid()}"
    remove_tree(stage, root.parent / stage.name)
    stage.mkdir(parents=True, exist_ok=False)
    try:
        local_api_token = ensure_local_api_token()
        shutil.copytree(source / "backend", stage / "backend")
        shutil.copytree(source / "extension", stage / "extension")
        inject_extension_token(stage / "extension", local_api_token)
        shutil.copy2(source / "Install Extension.html", stage / "Install Extension.html")
        shutil.copy2(source / "TEAM_CONFIGURATION.txt", stage / "TEAM_CONFIGURATION.txt")

        stop_backend(root)
        root.mkdir(parents=True, exist_ok=True)
        remove_tree(root / "backend", root / "backend")
        remove_tree(root / "extension", root / "extension")
        shutil.move(str(stage / "backend"), str(root / "backend"))
        shutil.move(str(stage / "extension"), str(root / "extension"))
        render_guide(
            stage / "Install Extension.html",
            root / "Install Extension.html",
            root / "extension",
        )
        shutil.copy2(stage / "TEAM_CONFIGURATION.txt", root / "TEAM_CONFIGURATION.txt")

        setup_copy = root / "Medhunt-Setup.exe"
        current = Path(sys.executable).resolve()
        if os.path.normcase(str(current)) != os.path.normcase(str(setup_copy.resolve())):
            shutil.copy2(current, setup_copy)

        baseline = install_bundled_lookup_config(source / "lookup-config.env")
        enforce_central_storage_config(baseline)
        register_install(root, setup_copy)

        legacy_menu = legacy_start_menu_dir()
        menu = start_menu_dir()
        if legacy_menu != menu and legacy_menu.exists():
            shutil.rmtree(legacy_menu, ignore_errors=True)
        create_shortcut(
            menu / "Start Medhunt Service.lnk",
            root / "backend" / "RadixsolBackend.exe",
            "Start the local Medhunt service",
        )
        create_shortcut(
            menu / "Install Medhunt Browser Extension.lnk",
            root / "Install Extension.html",
            "Open the Medhunt browser-extension installation guide",
        )
        create_shortcut(
            menu / "Uninstall Medhunt.lnk",
            setup_copy,
            "Uninstall Medhunt",
            "--uninstall",
        )

        ready = launch_backend(root)
        if not silent:
            if ready:
                message(
                    "Medhunt was installed and its local service is running.\n\n"
                    "The browser-extension guide will open next. Complete its one-time Load unpacked step."
                )
            else:
                message(
                    "Medhunt was installed, but the local service did not become ready. Open Start > "
                    "Medhunt > Start Medhunt Service and check the log under "
                    f"{data_root() / 'logs'}.",
                    flags=MB_OK | MB_ICONWARNING,
                )
            try:
                os.startfile(root / "Install Extension.html")
                subprocess.Popen(["explorer.exe", str(root / "extension")])
            except OSError:
                pass
        return 0
    except Exception:
        message(
            "Medhunt could not be installed. The setup package may be incomplete or "
            "Windows may have blocked access to the installation folder.",
            flags=MB_OK | MB_ICONERROR,
        )
        return 3
    finally:
        if stage.exists():
            shutil.rmtree(stage, ignore_errors=True)


def uninstall_final(*, silent: bool = False) -> int:
    root = validate_exact(install_root(), local_app_data() / "Programs" / "Radixsol")
    remove_data = False
    if not silent:
        choice = message(
            "Uninstall Medhunt?\n\n"
            "Yes: uninstall and delete the local candidate database/configuration.\n"
            "No: uninstall the application but preserve local data.\n"
            "Cancel: keep Medhunt installed.",
            flags=MB_YESNOCANCEL | MB_ICONWARNING,
        )
        if choice == IDCANCEL:
            return 1
        remove_data = choice == IDYES

    stop_backend(root)
    unregister_install()
    menu = start_menu_dir()
    if menu.exists():
        shutil.rmtree(menu, ignore_errors=True)
    legacy_menu = legacy_start_menu_dir()
    if legacy_menu != menu and legacy_menu.exists():
        shutil.rmtree(legacy_menu, ignore_errors=True)
    if root.exists():
        shutil.rmtree(root, ignore_errors=True)
    if remove_data:
        local_data = validate_exact(data_root(), local_app_data() / "Radixsol")
        if local_data.exists():
            shutil.rmtree(local_data, ignore_errors=True)
    if not silent:
        message("Medhunt was uninstalled.")
    ctypes.windll.kernel32.MoveFileExW(str(Path(sys.executable).resolve()), None, MOVEFILE_DELAY_UNTIL_REBOOT)
    return 0


def begin_uninstall(*, silent: bool = False) -> int:
    temporary = Path(tempfile.gettempdir()) / f"Medhunt-Uninstall-{os.getpid()}.exe"
    shutil.copy2(Path(sys.executable).resolve(), temporary)
    args = [str(temporary), "--uninstall-final"]
    if silent:
        args.append("--silent")
    subprocess.Popen(args, close_fds=True)
    return 0


def main() -> int:
    args = {arg.casefold() for arg in sys.argv[1:]}
    silent = "--silent" in args
    if "--uninstall-final" in args:
        return uninstall_final(silent=silent)
    if "--uninstall" in args:
        return begin_uninstall(silent=silent)
    return install(silent=silent)


if __name__ == "__main__":
    raise SystemExit(main())
