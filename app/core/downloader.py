from __future__ import annotations

import ipaddress
import re
import socket
import uuid
from dataclasses import dataclass
from html import unescape
from http.client import HTTPResponse
from http.cookiejar import CookieJar
from pathlib import Path
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, unquote, urlparse, urljoin
from urllib.request import (
    HTTPCookieProcessor,
    HTTPRedirectHandler,
    Request,
    build_opener,
)

from app.utils.paths import temp_root

ProgressCb = Callable[[int, int], None]  # received, total(-1 if unknown)

MAX_BYTES = 512 * 1024 * 1024  # 512 MB safety cap
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)
MAX_REDIRECTS = 5

_GDRIVE_FILE_RE = re.compile(
    r"https?://(?:drive|docs)\.google\.com/file/d/([a-zA-Z0-9_-]+)",
    re.I,
)
_GDRIVE_OPEN_RE = re.compile(
    r"https?://(?:drive|docs)\.google\.com/open\?[^#]*\bid=([a-zA-Z0-9_-]+)",
    re.I,
)
_GDRIVE_UC_RE = re.compile(
    r"https?://(?:drive|docs)\.google\.com/uc\?[^#]*\bid=([a-zA-Z0-9_-]+)",
    re.I,
)
_GDRIVE_USERCONTENT_RE = re.compile(
    r"https?://drive\.usercontent\.google\.com/download\?[^#]*\bid=([a-zA-Z0-9_-]+)",
    re.I,
)

# Hosts allowed for Google Drive download flow only.
_GDRIVE_HOSTS = {
    "drive.google.com",
    "docs.google.com",
    "drive.usercontent.google.com",
}


class DownloadError(Exception):
    pass


@dataclass
class DownloadResult:
    path: Path
    source_url: str
    filename: str
    bytes_written: int


def extract_gdrive_id(url: str) -> str | None:
    url = (url or "").strip()
    for pattern in (
        _GDRIVE_FILE_RE,
        _GDRIVE_OPEN_RE,
        _GDRIVE_UC_RE,
        _GDRIVE_USERCONTENT_RE,
    ):
        m = pattern.search(url)
        if m:
            return m.group(1)
    if "drive.google.com" in url.lower() or "drive.usercontent.google.com" in url.lower():
        qs = parse_qs(urlparse(url).query)
        if "id" in qs and qs["id"]:
            return qs["id"][0]
    return None


def _is_blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if ip.is_private or ip.is_loopback or ip.is_link_local:
        return True
    if ip.is_reserved or ip.is_multicast or ip.is_unspecified:
        return True
    # Cloud metadata / common internal
    if ip.version == 4 and str(ip) in {"169.254.169.254", "0.0.0.0"}:
        return True
    return False


def assert_url_safe(url: str, *, allow_http: bool = False) -> None:
    """
    Block SSRF to localhost/private/metadata and non-http(s) schemes.
    """
    parsed = urlparse(url)
    scheme = (parsed.scheme or "").lower()
    if scheme not in {"https", "http"}:
        raise DownloadError("仅允许 http:// 或 https:// 链接")
    if scheme == "http" and not allow_http:
        raise DownloadError("出于安全考虑仅允许 https:// 下载链接（请改用 HTTPS）")

    # Reject embedded credentials (https://user:pass@host/...)
    if parsed.username is not None or parsed.password is not None:
        raise DownloadError("禁止在下载链接中携带用户名/密码")

    host = (parsed.hostname or "").strip().lower()
    if not host:
        raise DownloadError("链接缺少主机名")
    if host in {"localhost"} or host.endswith(".localhost") or host.endswith(".internal"):
        raise DownloadError("禁止访问本机/内网地址")
    # Literal IP in URL
    try:
        ip = ipaddress.ip_address(host)
        if _is_blocked_ip(ip):
            raise DownloadError("禁止访问本机/内网/保留地址")
    except ValueError:
        # Hostname — resolve and check all answers
        try:
            infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
        except socket.gaierror as e:
            raise DownloadError(f"无法解析主机名: {host}") from e
        if not infos:
            raise DownloadError(f"无法解析主机名: {host}")
        for info in infos:
            addr = info[4][0]
            try:
                ip = ipaddress.ip_address(addr)
            except ValueError:
                continue
            if _is_blocked_ip(ip):
                raise DownloadError(f"禁止访问解析到内网的主机: {host} -> {addr}")


class _SafeRedirectHandler(HTTPRedirectHandler):
    def __init__(self, allow_http: bool = False) -> None:
        super().__init__()
        self.allow_http = allow_http
        self._hops = 0

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        self._hops += 1
        if self._hops > MAX_REDIRECTS:
            raise DownloadError("重定向次数过多")
        # newurl may be relative
        absolute = urljoin(req.full_url, newurl)
        assert_url_safe(absolute, allow_http=self.allow_http)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _guess_ext_from_magic(data: bytes) -> str:
    if data.startswith(b"PK\x03\x04") or data.startswith(b"PK\x05\x06"):
        return ".zip"
    if data.startswith(b"7z\xbc\xaf\x27\x1c"):
        return ".7z"
    return ""


def _filename_from_cd(content_disposition: str | None) -> str:
    if not content_disposition:
        return ""
    m = re.search(r"filename\*\s*=\s*([^']*)''([^;]+)", content_disposition, re.I)
    if m:
        return unquote(m.group(2).strip().strip('"'))
    m = re.search(r'filename\s*=\s*"([^"]+)"', content_disposition, re.I)
    if m:
        return m.group(1)
    m = re.search(r"filename\s*=\s*([^;]+)", content_disposition, re.I)
    if m:
        return unquote(m.group(1).strip().strip('"'))
    return ""


def _filename_from_url(url: str) -> str:
    path = unquote(urlparse(url).path)
    name = Path(path).name
    if name and "." in name:
        return name
    return ""


def _safe_basename(name: str) -> str:
    """Prevent path traversal via Content-Disposition / URL filename."""
    name = (name or "").replace("\x00", "").strip().strip('"')
    name = Path(name.replace("\\", "/")).name  # drop any directory components
    name = name.split("?")[0].split("#")[0]
    if not name or name in {".", ".."}:
        return ""
    # Keep conservative charset
    name = re.sub(r"[^\w.\-()+\[\] ]+", "_", name, flags=re.UNICODE)
    return name[:180]


def _ensure_archive_name(name: str, head: bytes) -> str:
    name = _safe_basename(name) or f"download_{uuid.uuid4().hex[:8]}"
    lower = name.lower()
    if lower.endswith((".7z", ".zip", ".7zip")):
        return name
    ext = _guess_ext_from_magic(head)
    if ext:
        return name + ext
    return name + ".zip"


def _is_html_bytes(data: bytes) -> bool:
    sample = data[:1024].lstrip().lower()
    return sample.startswith(b"<!doctype html") or sample.startswith(b"<html")


def _parse_gdrive_confirm(html: str) -> tuple[str, str] | None:
    confirm = ""
    file_uuid = ""
    m = re.search(r'name="confirm"\s+value="([^"]+)"', html, re.I)
    if m:
        confirm = unescape(m.group(1))
    m = re.search(r'name="uuid"\s+value="([^"]+)"', html, re.I)
    if m:
        file_uuid = unescape(m.group(1))
    if confirm:
        return confirm, file_uuid
    m = re.search(r"confirm=([0-9A-Za-z_]+)&", html)
    if m:
        return m.group(1), ""
    return None


def _opener(allow_http: bool = False):
    jar = CookieJar()
    return build_opener(
        HTTPCookieProcessor(jar),
        _SafeRedirectHandler(allow_http=allow_http),
    ), jar


def _request(url: str) -> Request:
    return Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "*/*",
        },
    )


def _stream_to_file(
    resp: HTTPResponse,
    dest: Path,
    progress: ProgressCb | None = None,
) -> tuple[int, bytes]:
    total = -1
    try:
        total = int(resp.headers.get("Content-Length") or -1)
    except ValueError:
        total = -1

    dest.parent.mkdir(parents=True, exist_ok=True)
    received = 0
    head = b""
    with open(dest, "wb") as out:
        while True:
            chunk = resp.read(1024 * 64)
            if not chunk:
                break
            if len(head) < 64:
                need = 64 - len(head)
                head += chunk[:need]
            received += len(chunk)
            if received > MAX_BYTES:
                raise DownloadError(f"文件超过大小限制（{MAX_BYTES // (1024 * 1024)} MB）")
            out.write(chunk)
            if progress:
                progress(received, total)
    return received, head


def download_gdrive(file_id: str, dest_dir: Path, progress: ProgressCb | None = None) -> DownloadResult:
    # Validate crafted IDs are alphanumeric-ish
    if not re.fullmatch(r"[a-zA-Z0-9_-]{10,128}", file_id or ""):
        raise DownloadError("无效的 Google Drive 文件 ID")

    opener, _jar = _opener(allow_http=False)
    first_url = f"https://drive.google.com/uc?export=download&id={file_id}"
    assert_url_safe(first_url, allow_http=False)
    try:
        resp = opener.open(_request(first_url), timeout=60)
    except (HTTPError, URLError, DownloadError) as e:
        raise DownloadError(f"无法访问 Google Drive: {e}") from e

    content_type = (resp.headers.get("Content-Type") or "").lower()
    cd = resp.headers.get("Content-Disposition")
    filename = _filename_from_cd(cd)

    if "attachment" in (cd or "").lower() or "octet-stream" in content_type or "zip" in content_type:
        tmp = dest_dir / f"gdrive_{uuid.uuid4().hex}.part"
        written, head = _stream_to_file(resp, tmp, progress)
        final_name = _ensure_archive_name(filename or f"gdrive_{file_id[:12]}", head)
        final_path = dest_dir / final_name
        if final_path.exists():
            final_path.unlink()
        tmp.replace(final_path)
        return DownloadResult(final_path, first_url, final_name, written)

    data = resp.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise DownloadError(f"文件超过大小限制（{MAX_BYTES // (1024 * 1024)} MB）")

    if not _is_html_bytes(data):
        head = data[:64]
        final_name = _ensure_archive_name(filename or f"gdrive_{file_id[:12]}", head)
        final_path = dest_dir / final_name
        final_path.write_bytes(data)
        if progress:
            progress(len(data), len(data))
        return DownloadResult(final_path, first_url, final_name, len(data))

    html = data.decode("utf-8", errors="ignore")
    parsed = _parse_gdrive_confirm(html)
    if not parsed:
        confirm, file_uuid = "t", ""
    else:
        confirm, file_uuid = parsed

    # Only allow known Google hosts for the confirm hop
    second = (
        f"https://drive.usercontent.google.com/download"
        f"?id={file_id}&export=download&confirm={confirm}"
    )
    if file_uuid:
        second += f"&uuid={file_uuid}"
    assert_url_safe(second, allow_http=False)
    if urlparse(second).hostname not in _GDRIVE_HOSTS:
        raise DownloadError("Google Drive 确认下载跳转到了非预期主机")

    try:
        resp2 = opener.open(_request(second), timeout=120)
    except (HTTPError, URLError, DownloadError) as e:
        raise DownloadError(f"Google Drive 确认下载失败: {e}") from e

    cd2 = resp2.headers.get("Content-Disposition")
    filename2 = _filename_from_cd(cd2) or filename or f"gdrive_{file_id[:12]}"
    tmp = dest_dir / f"gdrive_{uuid.uuid4().hex}.part"
    written, head = _stream_to_file(resp2, tmp, progress)
    if written < 100 and _is_html_bytes(tmp.read_bytes()[:200]):
        raise DownloadError(
            "Google Drive 下载到的是网页而不是文件。\n"
            "请确认链接是「任何拥有链接的人都可以查看」，或改用直接下载链接。"
        )
    final_name = _ensure_archive_name(filename2, head)
    final_path = dest_dir / final_name
    if final_path.exists():
        final_path.unlink()
    tmp.replace(final_path)
    return DownloadResult(final_path, second, final_name, written)


def download_http(url: str, dest_dir: Path, progress: ProgressCb | None = None) -> DownloadResult:
    assert_url_safe(url, allow_http=False)
    opener, _ = _opener(allow_http=False)
    try:
        resp = opener.open(_request(url), timeout=120)
    except (HTTPError, URLError, DownloadError) as e:
        raise DownloadError(f"下载失败: {e}") from e

    final_url = resp.geturl()
    assert_url_safe(final_url, allow_http=False)

    cd = resp.headers.get("Content-Disposition")
    filename = _filename_from_cd(cd) or _filename_from_url(final_url) or _filename_from_url(url)
    tmp = dest_dir / f"dl_{uuid.uuid4().hex}.part"
    written, head = _stream_to_file(resp, tmp, progress)

    if written < 512 * 1024:
        sample = tmp.read_bytes()[:1024]
        if _is_html_bytes(sample):
            tmp.unlink(missing_ok=True)
            raise DownloadError(
                "下载到的是网页，不是压缩包。请使用直接下载链接，或 Google Drive 文件分享链接。"
            )

    final_name = _ensure_archive_name(filename, head)
    final_path = dest_dir / final_name
    if final_path.exists():
        final_path.unlink()
    tmp.replace(final_path)
    return DownloadResult(final_path, final_url, final_name, written)


def download_archive(url: str, progress: ProgressCb | None = None) -> DownloadResult:
    """
    Download an archive from Google Drive file URL or a direct https link.
    Blocks private/localhost targets (SSRF) and requires HTTPS.
    """
    url = (url or "").strip()
    if not url:
        raise DownloadError("链接为空")

    dest_dir = temp_root() / f"download_{uuid.uuid4().hex}"
    dest_dir.mkdir(parents=True, exist_ok=True)

    g_id = extract_gdrive_id(url)
    if g_id:
        return download_gdrive(g_id, dest_dir, progress)

    assert_url_safe(url, allow_http=False)
    return download_http(url, dest_dir, progress)
