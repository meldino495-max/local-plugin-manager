"""Register Start Menu / App Paths so Windows Search can find the app."""

from __future__ import annotations

import base64
import logging
import os
import subprocess
import sys
from pathlib import Path

from app import __app_name__
from app.utils.paths import app_icon_path, load_app_config, save_app_config

log = logging.getLogger(__name__)

_CFG_KEY = "shell_registered_exe"
_START_MENU_NAME = f"{__app_name__}.lnk"
_START_MENU_ALIAS = "LocalPluginManager.lnk"
_LAUNCHER_DIR_NAME = "LocalPluginManager"


def _is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def _start_menu_programs() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return Path(base) / "Microsoft" / "Windows" / "Start Menu" / "Programs"


def _launcher_dir() -> Path:
    local = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(local) / _LAUNCHER_DIR_NAME


def _write_ascii_launcher(exe: Path) -> tuple[Path, Path]:
    """
    WScript.Shell often rejects TargetPath under non-ASCII directories.
    Keep an ASCII-path .cmd launcher that starts the real (possibly Chinese) exe.
    """
    launch_dir = _launcher_dir()
    launch_dir.mkdir(parents=True, exist_ok=True)

    ico_src = app_icon_path()
    ico_dst = launch_dir / "icon.ico"
    if ico_src.is_file() and ico_src.suffix.lower() == ".ico":
        ico_dst.write_bytes(ico_src.read_bytes())
    else:
        ico_dst = exe  # IconLocation can reference the exe

    run_cmd = launch_dir / "run.cmd"
    # Use `start ""` so cmd does not treat the quoted exe path as a window title.
    run_cmd.write_text(
        "\r\n".join(
            [
                "@echo off",
                "REM Auto-generated launcher for Windows Search / Start Menu",
                f'start "" "{exe}"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    return run_cmd, ico_dst


def _ps_quote(value: str) -> str:
    """Single-quoted PowerShell string with escaped quotes."""
    return "'" + value.replace("'", "''") + "'"


def _create_shortcut(
    lnk_path: Path,
    target: Path,
    workdir: Path,
    description: str,
    icon: Path,
) -> None:
    """
    Create a .lnk via WScript (ASCII temp path), then rename to the final name.

    Some Windows COM hosts fail when CreateShortcut/Save receives a non-ASCII
    .lnk path, even with -EncodedCommand. Python's Path.rename handles Unicode.
    """
    lnk_path.parent.mkdir(parents=True, exist_ok=True)
    icon_loc = f"{icon},0" if icon.suffix.lower() == ".exe" else str(icon)

    # Always write through an ASCII temp name in the same folder.
    tmp_lnk = lnk_path.parent / f"_lpm_tmp_{os.getpid()}.lnk"
    tmp_lnk.unlink(missing_ok=True)

    body = (
        "$ErrorActionPreference = 'Stop'; "
        f"$lnkPath = {_ps_quote(str(tmp_lnk))}; "
        f"$target = {_ps_quote(str(target))}; "
        f"$workDir = {_ps_quote(str(workdir))}; "
        f"$desc = {_ps_quote(description)}; "
        f"$ico = {_ps_quote(icon_loc)}; "
        "$w = New-Object -ComObject WScript.Shell; "
        "$s = $w.CreateShortcut($lnkPath); "
        "$s.TargetPath = $target; "
        "$s.WorkingDirectory = $workDir; "
        "$s.IconLocation = $ico; "
        "$s.Description = $desc; "
        "$s.Save()"
    )
    encoded = base64.b64encode(body.encode("utf-16-le")).decode("ascii")
    try:
        r = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-EncodedCommand",
                encoded,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if r.returncode != 0 or not tmp_lnk.is_file():
            err = (r.stderr or r.stdout or f"exit {r.returncode}").strip()
            raise RuntimeError(err or "shortcut creation failed")
        lnk_path.unlink(missing_ok=True)
        tmp_lnk.replace(lnk_path)
    finally:
        tmp_lnk.unlink(missing_ok=True)


def _register_app_paths(exe: Path) -> None:
    try:
        import winreg
    except ImportError:
        return

    names = [exe.name, "LocalPluginManager.exe"]
    seen: set[str] = set()
    for name in names:
        if name in seen:
            continue
        seen.add(name)
        key_path = rf"Software\Microsoft\Windows\CurrentVersion\App Paths\{name}"
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, key_path) as key:
            winreg.SetValueEx(key, None, 0, winreg.REG_SZ, str(exe))
            winreg.SetValueEx(key, "Path", 0, winreg.REG_SZ, str(exe.parent))


def ensure_start_menu_registration() -> bool:
    """
    Idempotent: create Start Menu .lnk + App Paths for the frozen exe.
    Re-runs when the exe moves. Best-effort; never raises to callers.
    """
    if sys.platform != "win32" or not _is_frozen():
        return False

    exe = Path(sys.executable).resolve()
    if not exe.is_file():
        return False

    cfg = load_app_config()
    programs = _start_menu_programs()
    primary = programs / _START_MENU_NAME
    if cfg.get(_CFG_KEY) == str(exe) and primary.is_file():
        return True

    try:
        run_cmd, icon = _write_ascii_launcher(exe)
        _create_shortcut(
            primary,
            target=run_cmd,
            workdir=run_cmd.parent,
            description=__app_name__,
            icon=icon if icon.suffix.lower() == ".ico" else exe,
        )
        _create_shortcut(
            programs / _START_MENU_ALIAS,
            target=run_cmd,
            workdir=run_cmd.parent,
            description="Local Plugin Manager / 本地插件管理器",
            icon=icon if icon.suffix.lower() == ".ico" else exe,
        )
        _register_app_paths(exe)
        cfg[_CFG_KEY] = str(exe)
        save_app_config(cfg)
        log.info("Registered Start Menu shortcuts for %s", exe)
        return True
    except Exception:
        log.exception("Failed to register app for Windows Search")
        return False
