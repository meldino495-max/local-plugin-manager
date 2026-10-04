"""Security-focused regression checks (no network)."""

from __future__ import annotations

import json
import sys
import tempfile
import zipfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.core.archive import ArchiveError, extract_archive
from app.core.downloader import DownloadError, assert_url_safe, extract_gdrive_id


def test_zip_slip_rejected() -> None:
    base = Path(tempfile.mkdtemp(prefix="bem_sec_"))
    zpath = base / "evil.zip"
    with zipfile.ZipFile(zpath, "w") as zf:
        zf.writestr("../evil.txt", "x")
        zf.writestr("C:/Windows/evil.txt", "y")
    out = base / "out"
    try:
        extract_archive(zpath, out)
        raise AssertionError("zip slip should fail")
    except ArchiveError:
        pass
    # must not create escaped file next to out
    assert not (base / "evil.txt").exists()


def test_ssrf_blocked() -> None:
    for url in (
        "http://127.0.0.1/x",
        "https://127.0.0.1/x",
        "https://localhost/x",
        "https://169.254.169.254/latest/meta-data/",
        "file:///C:/Windows/win.ini",
        "https://user:pass@example.com/a.zip",
    ):
        try:
            assert_url_safe(url, allow_http=True)
            raise AssertionError(f"should block {url}")
        except DownloadError:
            pass
    try:
        assert_url_safe("http://example.com/a.zip", allow_http=False)
        raise AssertionError("plain http should be blocked")
    except DownloadError:
        pass


def test_gdrive_id() -> None:
    assert (
        extract_gdrive_id("https://drive.google.com/file/d/1AbCDefGhIJkLMN/view")
        == "1AbCDefGhIJkLMN"
    )


def test_safe_zip_ok() -> None:
    base = Path(tempfile.mkdtemp(prefix="bem_sec_ok_"))
    zpath = base / "ok.zip"
    with zipfile.ZipFile(zpath, "w") as zf:
        zf.writestr(
            "manifest.json",
            json.dumps({"name": "T", "version": "1", "manifest_version": 3}),
        )
    out = base / "out"
    extract_archive(zpath, out)
    assert (out / "manifest.json").is_file()


if __name__ == "__main__":
    test_zip_slip_rejected()
    print("zip_slip_ok")
    test_ssrf_blocked()
    print("ssrf_ok")
    test_gdrive_id()
    print("gdrive_ok")
    test_safe_zip_ok()
    print("safe_zip_ok")
    print("ALL_PASS")
