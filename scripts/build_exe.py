"""
Build 本地插件管理器.exe with PyInstaller (Windows).

Usage (from repo root):
  python scripts/build_exe.py
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "LocalPluginManager.spec"
DIST = ROOT / "dist"
BUILD = ROOT / "build"
EXE_NAME = "本地插件管理器.exe"


def _configure_stdio() -> None:
    # GitHub Actions Windows runners often use cp1252; avoid UnicodeEncodeError on Chinese paths.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


def _log(msg: str, *, file=None) -> None:
    target = file or sys.stdout
    try:
        print(msg, file=target)
    except UnicodeEncodeError:
        print(msg.encode("ascii", "backslashreplace").decode("ascii"), file=target)


def main() -> int:
    _configure_stdio()
    if sys.platform != "win32":
        _log("This project currently ships a Windows exe only.", file=sys.stderr)
        return 1
    if not SPEC.is_file():
        _log(f"missing {SPEC}", file=sys.stderr)
        return 1

    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        _log("Installing build dependencies...")
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "-r", str(ROOT / "requirements-build.txt")]
        )

    # Clean previous artifacts for a reproducible one-file build
    for path in (DIST, BUILD):
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)

    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        str(SPEC),
    ]
    _log("Running: " + " ".join(cmd))
    subprocess.check_call(cmd, cwd=str(ROOT))

    exe = DIST / EXE_NAME
    if not exe.is_file():
        _log("Build finished but exe not found: " + str(exe), file=sys.stderr)
        if DIST.is_dir():
            _log("dist contents: " + repr([p.name for p in DIST.iterdir()]), file=sys.stderr)
        return 1
    size_mb = exe.stat().st_size / (1024 * 1024)
    _log(f"OK: {exe.name} ({size_mb:.1f} MB) -> {exe.parent}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
