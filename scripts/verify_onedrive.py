"""Live/offline checks for OneDrive badger download flow."""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.core.downloader import (
    DownloadError,
    _parse_onedrive_access,
    _sharepoint_force_download_url,
    download_onedrive,
    is_onedrive_url,
)
from app.utils.paths import ensure_runtime_dirs, temp_root


def test_offline() -> None:
    assert is_onedrive_url("https://1drv.ms/u/c/abcdef/TOKEN")
    sp = _sharepoint_force_download_url(
        "https://contoso-my.sharepoint.com/:u:/g/personal/alice_contoso_com/AbCdEfToken123"
    )
    assert sp is not None
    assert "_layouts/15/download.aspx?share=AbCdEfToken123" in sp
    assert "personal/alice_contoso_com" in sp

    acc = _parse_onedrive_access(
        "https://onedrive.live.com/?id=CID!item&cid=CID&redeem=aHR0cHM6Ly8xZHJ2Lm1zL3U"
    )
    assert acc.redeem.startswith("aHR0")
    assert acc.resid == "CID!item"
    assert acc.container_id == "CID"
    print("offline_ok")


def test_live_sample() -> None:
    """Public Microsoft sample share (video). Confirms Badger + driveitem path."""
    ensure_runtime_dirs()
    url = "https://1drv.ms/v/c/16736b8f8892fe5e/EeWQ4oZ_sZtGmTFmsnFDU3oBM7cJoyL9tapTJU7gxZznzA"
    dest = temp_root() / "onedrive_probe"
    dest.mkdir(parents=True, exist_ok=True)
    try:
        result = download_onedrive(url, dest)
    except DownloadError as e:
        print("LIVE_FAIL", str(e)[:200])
        raise
    print("LIVE_OK", result.filename, result.bytes_written)
    assert result.bytes_written > 1000
    assert result.path.is_file()
    # cleanup
    try:
        result.path.unlink(missing_ok=True)
    except OSError:
        pass


if __name__ == "__main__":
    test_offline()
    test_live_sample()
    print("ALL_PASS")
