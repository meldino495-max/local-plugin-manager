"""
Build LocalPluginManager.exe with PyInstaller (Windows).

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


def main() -> int:
    if sys.platform != "win32":
        print("This project currently ships a Windows exe only.", file=sys.stderr)
        return 1
    if not SPEC.is_file():
        print(f"missing {SPEC}", file=sys.stderr)
        return 1

    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("Installing build dependencies…")
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
    print("Running:", " ".join(cmd))
    subprocess.check_call(cmd, cwd=str(ROOT))

    exe = DIST / "LocalPluginManager.exe"
    if not exe.is_file():
        print("Build finished but exe not found:", exe, file=sys.stderr)
        return 1
    size_mb = exe.stat().st_size / (1024 * 1024)
    print(f"OK: {exe} ({size_mb:.1f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
