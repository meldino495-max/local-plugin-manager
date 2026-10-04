"""
Create an Explorer shortcut that shows the app icon.

WScript.Shell rejects some TargetPath values under non-ASCII directories, so we
maintain a small ASCII launcher at ../LocalPluginManager/ and point the .lnk there.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    root = _ROOT.resolve()
    ico_src = root / "resources" / "icon.ico"
    if not ico_src.is_file():
        print("missing resources/icon.ico", file=sys.stderr)
        return 1

    launch_dir = root.parent / "LocalPluginManager"
    launch_dir.mkdir(parents=True, exist_ok=True)
    ico_dst = launch_dir / "icon.ico"
    ico_dst.write_bytes(ico_src.read_bytes())

    run_bat = launch_dir / "run.bat"
    # Relative path: LocalPluginManager/ -> sibling 本地插件管理器/
    run_bat.write_text(
        "\r\n".join(
            [
                "@echo off",
                "REM Portable launcher (relative to sibling project folder)",
                'cd /d "%~dp0..\\本地插件管理器"',
                'if exist ".venv\\Scripts\\pythonw.exe" (',
                '  start "" ".venv\\Scripts\\pythonw.exe" "main.py"',
                ") else (",
                '  call "打开.bat"',
                ")",
                "",
            ]
        ),
        encoding="utf-8",
    )

    lnk_ascii = launch_dir / "Local Plugin Manager.lnk"
    lnk_proj = root / "LocalPluginManager.lnk"

    ps1 = root / "scripts" / "_tmp_make_lnk.ps1"
    body = (
        "$ErrorActionPreference = 'Stop'\r\n"
        f"$lnkPath = @'\r\n{lnk_ascii}\r\n'@\r\n"
        f"$target = @'\r\n{run_bat}\r\n'@\r\n"
        f"$workDir = @'\r\n{launch_dir}\r\n'@\r\n"
        f"$ico = @'\r\n{ico_dst}\r\n'@\r\n"
        "$lnkPath=$lnkPath.Trim(); $target=$target.Trim(); $workDir=$workDir.Trim(); $ico=$ico.Trim()\r\n"
        "$w = New-Object -ComObject WScript.Shell\r\n"
        "$s = $w.CreateShortcut($lnkPath)\r\n"
        "$s.TargetPath = $target\r\n"
        "$s.WorkingDirectory = $workDir\r\n"
        "$s.IconLocation = $ico\r\n"
        "$s.Description = 'Local Plugin Manager'\r\n"
        "$s.Save()\r\n"
        "Write-Output $lnkPath\r\n"
    )
    ps1.write_bytes(body.encode("utf-16"))
    try:
        r = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(ps1),
            ],
            capture_output=True,
        )
        sys.stdout.buffer.write(r.stdout)
        sys.stderr.buffer.write(r.stderr)
        if r.returncode != 0:
            return r.returncode
    finally:
        ps1.unlink(missing_ok=True)

    # Mirror into project folder for convenience
    lnk_proj.write_bytes(lnk_ascii.read_bytes())
    print("created", lnk_ascii)
    print("created", lnk_proj)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
