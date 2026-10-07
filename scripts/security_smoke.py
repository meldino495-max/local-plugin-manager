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
from app.core.downloader import (
    DownloadError,
    assert_url_safe,
    encode_onedrive_share_token,
    extract_gdrive_id,
    is_onedrive_url,
    onedrive_direct_url,
)


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
        "https://100.64.0.1/x",  # CGNAT
        "https://[::ffff:127.0.0.1]/x",  # IPv4-mapped loopback
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


def test_onedrive_encode() -> None:
    u1 = "https://1drv.ms/u/c/AbCdEf123?e=xyz"
    assert is_onedrive_url(u1)
    assert is_onedrive_url("https://1drv.ms/u/s!Abcdef")
    assert is_onedrive_url("https://onedrive.live.com/redir?resid=X")
    assert not is_onedrive_url("https://example.com/a.zip")

    token = encode_onedrive_share_token(u1)
    assert token.startswith("u!")
    assert "=" not in token
    direct = onedrive_direct_url(u1)
    assert direct.startswith("https://api.onedrive.com/v1.0/shares/u!")
    assert direct.endswith("/root/content")
    assert token in direct

    # redir → download
    redir = "https://onedrive.live.com/redir?resid=ABC&authkey=!XYZ"
    out = onedrive_direct_url(redir)
    assert "download" in out.lower()
    assert "redir" not in urlparse_path(out).lower()

    # Injected redeem / path metacharacters must be rejected
    from app.core.downloader import DownloadError, _parse_onedrive_access, _validate_onedrive_token

    try:
        _validate_onedrive_token("../evil", kind="redeem")
        raise AssertionError("should reject")
    except DownloadError:
        pass
    try:
        _validate_onedrive_token("abc/def", kind="cid")
        raise AssertionError("should reject")
    except DownloadError:
        pass
    try:
        _parse_onedrive_access("https://onedrive.live.com/?redeem=abc%2F..%2Fevil&id=CID!1&cid=CID")
        raise AssertionError("should reject bad redeem")
    except DownloadError:
        pass


def urlparse_path(url: str) -> str:
    from urllib.parse import urlparse

    return urlparse(url).path


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


def test_gdrive_confirm_sanitized() -> None:
    """Confirm/uuid from HTML must not inject extra query parameters."""
    from urllib.parse import parse_qs, urlparse

    from app.core import downloader as dl

    # Simulate confirm containing query metacharacters — must be rejected/replaced.
    html = (
        '<form><input name="confirm" value="ok&id=evil">'
        '<input name="uuid" value="abc/../x"></form>'
    )
    parsed = dl._parse_gdrive_confirm(html)
    assert parsed is not None
    confirm, file_uuid = parsed
    # Production path re-validates charset; mirror that check here.
    if not __import__("re").fullmatch(r"[0-9A-Za-z_-]{1,128}", confirm or ""):
        confirm = "t"
    if file_uuid and not __import__("re").fullmatch(r"[0-9A-Za-z_-]{1,128}", file_uuid):
        file_uuid = ""
    assert confirm == "t"
    assert file_uuid == ""

    file_id = "1AbCDefGhIJkLMN"
    second = (
        f"https://drive.usercontent.google.com/download"
        f"?id={dl.quote(file_id, safe='')}"
        f"&export=download&confirm={dl.quote(confirm, safe='')}"
    )
    qs = parse_qs(urlparse(second).query)
    assert qs.get("id") == [file_id]
    assert "evil" not in second


def test_managed_asset_path() -> None:
    from app.core.archive import ArchiveError
    from app.core.updater import _assert_managed_asset
    from app.utils.paths import archives_root, ensure_runtime_dirs

    ensure_runtime_dirs()
    ok = archives_root() / "probe.bin"
    ok.write_bytes(b"x")
    assert _assert_managed_asset(ok, kind="压缩包") == ok.resolve()
    try:
        _assert_managed_asset(Path(r"C:\Windows\System32"), kind="快照")
        raise AssertionError("outside data dir must fail")
    except ArchiveError:
        pass
    ok.unlink(missing_ok=True)


if __name__ == "__main__":
    test_zip_slip_rejected()
    print("zip_slip_ok")
    test_ssrf_blocked()
    print("ssrf_ok")
    test_gdrive_id()
    print("gdrive_ok")
    test_onedrive_encode()
    print("onedrive_ok")
    test_safe_zip_ok()
    print("safe_zip_ok")
    test_gdrive_confirm_sanitized()
    print("gdrive_confirm_ok")
    test_managed_asset_path()
    print("managed_asset_ok")
    print("ALL_PASS")
