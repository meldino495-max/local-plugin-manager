from __future__ import annotations

import base64
import ipaddress
import json
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
from urllib.parse import parse_qs, unquote, urlparse, urljoin, urlunparse, urlencode, quote
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
MAX_REDIRECTS = 8

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


def is_onedrive_url(url: str) -> bool:
    """Detect OneDrive / 1drv.ms / onedrive.live / SharePoint file share links."""
    url = (url or "").strip()
    if not url:
        return False
    host = (urlparse(url).hostname or "").lower()
    if not host:
        return False
    if host in {"1drv.ms", "onedrive.live.com", "api.onedrive.com"}:
        return True
    if host.endswith(".1drv.com") or host.endswith(".onedrive.com"):
        return True
    if host.endswith(".sharepoint.com") or host.endswith(".sharepoint-df.com"):
        path = urlparse(url).path.lower()
        if "/:u:/" in path or "/:f:/" in path or "/:w:/" in path or "/:x:/" in path:
            return True
        if "download.aspx" in path or "guestaccess.aspx" in path:
            return True
    return False


def encode_onedrive_share_token(share_url: str) -> str:
    """
    Encode a sharing URL as OneDrive 'u!' share token.
    See: https://learn.microsoft.com/en-us/onedrive/developer/rest-api/api/shares_get
    """
    raw = (share_url or "").strip().encode("utf-8")
    # urlsafe_b64 uses -/_ ; strip padding '=' as required by the API
    return "u!" + base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


# Browser-anonymous Badger credentials used by OneDrive web for public shares
# (required for new 1drv.ms/u/c/... links after Microsoft migration).
# These are public client identifiers shipped by Microsoft browsers — not app secrets.
_BADGER_TOKEN_URL = "https://api-badgerp.svc.ms/v1.0/token"
_BADGER_APP_ID = "1141147648"
_BADGER_APP_UUID = "5cbed6ac-a083-4e14-b191-b4ba07653de2"
_PERSONAL_SHARES_API = "https://my.microsoftpersonalcontent.com/_api/v2.0/shares"

# After following a share link / resolving @content.downloadUrl, only talk to
# Microsoft OneDrive / SharePoint related hosts (defense-in-depth vs open redirect).
_MS_DOWNLOAD_HOST_SUFFIXES = (
    "1drv.ms",
    "1drv.com",
    "onedrive.live.com",
    "onedrive.com",
    "api.onedrive.com",
    "sharepoint.com",
    "sharepoint.cn",
    "sharepoint-df.com",
    "microsoftpersonalcontent.com",
    "svc.ms",
    "live.com",
)


def _is_ms_onedrive_host(host: str | None) -> bool:
    host = (host or "").lower().strip(".")
    if not host:
        return False
    return any(host == s or host.endswith("." + s) for s in _MS_DOWNLOAD_HOST_SUFFIXES)


def _assert_ms_onedrive_url(url: str, *, what: str = "OneDrive") -> None:
    assert_url_safe(url, allow_http=False)
    host = urlparse(url).hostname
    if not _is_ms_onedrive_host(host):
        raise DownloadError(f"{what} 跳转到了非微软下载域名，已中止: {host or '?'}")


def _validate_onedrive_token(value: str, *, kind: str) -> str:
    """Reject path/query metacharacters in IDs interpolated into request URLs."""
    value = (value or "").strip()
    if not value:
        raise DownloadError(f"OneDrive {kind} 为空")
    patterns = {
        "redeem": r"[A-Za-z0-9_-]{8,2048}",
        "resid": r"[A-Za-z0-9!._-]{3,256}",
        "cid": r"[A-Za-z0-9]{3,128}",
        "authkey": r"!?[A-Za-z0-9_-]{3,256}",
        "share_user": r"[A-Za-z0-9._-]{1,256}",
        "share_token": r"[A-Za-z0-9_-]{6,512}",
    }
    pat = patterns.get(kind)
    if not pat or not re.fullmatch(pat, value):
        raise DownloadError(f"OneDrive {kind} 含非法字符，已拒绝")
    return value


@dataclass
class _OneDriveAccess:
    container_id: str = ""
    resid: str = ""
    auth_key: str = ""
    redeem: str = ""
    expanded_url: str = ""


def onedrive_direct_url(share_url: str) -> str:
    """
    Best-effort URL transform for classic links / tests.
    New 1drv.ms/u/c links need the Badger flow in download_onedrive(); this
    helper alone is often insufficient (api.onedrive.com may return 401).
    """
    url = (share_url or "").strip()
    if not url:
        raise DownloadError("OneDrive 链接为空")

    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    path_l = (parsed.path or "").lower()

    if host == "api.onedrive.com" and path_l.rstrip("/").endswith("/content"):
        return url

    if host == "onedrive.live.com":
        qs = parse_qs(parsed.query)
        if "download" in path_l or qs.get("download"):
            return url
        if "redir" in path_l or "redir" in (parsed.query or "").lower():
            new_path = parsed.path.replace("redir", "download").replace("Redir", "download")
            if "download" not in new_path.lower():
                new_path = "/download"
            return urlunparse(
                ("https", parsed.netloc, new_path, parsed.params, parsed.query, "")
            )

    if host.endswith(".1drv.com") and host != "1drv.ms":
        return url

    sp = _sharepoint_force_download_url(url)
    if sp:
        return sp

    token = encode_onedrive_share_token(url)
    return f"https://api.onedrive.com/v1.0/shares/{token}/root/content"


def _sharepoint_force_download_url(url: str) -> str | None:
    """
    Convert SharePoint / OneDrive-business share pages into a download URL.
    """
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if not (
        host.endswith("sharepoint.com")
        or host.endswith("sharepoint.cn")
        or host.endswith("sharepoint-df.com")
    ):
        return None

    path = parsed.path or ""
    path_l = path.lower()
    if "/:f:/" in path_l:
        raise DownloadError("这是 OneDrive/SharePoint「文件夹」分享链接，请改为分享单个压缩包文件。")

    # /:u:/g/personal/{user}/{shareToken}
    m = re.search(r"/:[uwxi]:/g/personal/([^/]+)/([^/?#]+)", path, re.I)
    if m:
        user = _validate_onedrive_token(unquote(m.group(1)), kind="share_user")
        token = _validate_onedrive_token(unquote(m.group(2)), kind="share_token")
        return (
            f"https://{host}/personal/{quote(user, safe='._-')}"
            f"/_layouts/15/download.aspx?share={quote(token, safe='_-')}"
        )

    # /:u:/r/personal/.../Documents/file?web=1 → download=1
    qs = dict(parse_qs(parsed.query, keep_blank_values=True))
    flat = {k: (v[0] if isinstance(v, list) and v else "") for k, v in qs.items()}
    if "web" in flat:
        flat.pop("web", None)
    flat["download"] = "1"
    return urlunparse(
        (
            "https",
            parsed.netloc,
            parsed.path,
            parsed.params,
            urlencode(flat),
            "",
        )
    )


def _parse_onedrive_access(expanded_url: str) -> _OneDriveAccess:
    parsed = urlparse(expanded_url)
    qs = parse_qs(parsed.query)
    resid = (qs.get("resid") or [""])[0]
    redeem = (qs.get("redeem") or [""])[0]
    auth_key = (qs.get("authkey") or [""])[0]
    id_ = (qs.get("id") or [""])[0]
    cid = (qs.get("cid") or [""])[0]
    if not resid and "!" in id_:
        resid = id_
    if not cid and resid and "!" in resid:
        cid = resid.split("!", 1)[0]

    # Validate only fields that are present — empty means unused path
    if redeem:
        redeem = _validate_onedrive_token(redeem, kind="redeem")
    if resid:
        resid = _validate_onedrive_token(resid, kind="resid")
    if cid:
        cid = _validate_onedrive_token(cid, kind="cid")
    if auth_key:
        auth_key = _validate_onedrive_token(auth_key, kind="authkey")

    return _OneDriveAccess(
        container_id=cid,
        resid=resid,
        auth_key=auth_key,
        redeem=redeem,
        expanded_url=expanded_url,
    )


def _follow_to_final_url(opener, url: str) -> str:
    _assert_ms_onedrive_url(url, what="OneDrive 起始链接")
    try:
        resp = opener.open(_request(url), timeout=60)
    except HTTPError as e:
        # Some OneDrive edges return 302 to a page that urllib already followed;
        # surface remaining errors clearly.
        raise DownloadError(f"无法打开 OneDrive 链接: HTTP {e.code}") from e
    except (URLError, DownloadError) as e:
        raise DownloadError(f"无法打开 OneDrive 链接: {e}") from e
    final = resp.geturl()
    try:
        resp.close()
    except Exception:
        pass
    _assert_ms_onedrive_url(final, what="OneDrive 重定向")
    return final


def _badger_token(opener) -> str:
    assert_url_safe(_BADGER_TOKEN_URL, allow_http=False)
    body = json.dumps({"appId": _BADGER_APP_UUID}).encode("utf-8")
    req = Request(
        _BADGER_TOKEN_URL,
        data=body,
        headers={
            "User-Agent": USER_AGENT,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "AppId": _BADGER_APP_ID,
        },
        method="POST",
    )
    try:
        resp = opener.open(req, timeout=30)
        data = json.loads(resp.read().decode("utf-8", errors="replace"))
    except HTTPError as e:
        raise DownloadError(f"无法获取 OneDrive 临时访问令牌: HTTP {e.code}") from e
    except Exception as e:
        raise DownloadError(f"无法获取 OneDrive 临时访问令牌: {e}") from e
    token = data.get("token") if isinstance(data, dict) else None
    if not token:
        raise DownloadError("OneDrive 临时访问令牌响应无效")
    return str(token)


def _driveitem_download_url(opener, access: _OneDriveAccess) -> tuple[str, str]:
    """
    Resolve @content.downloadUrl + file name for a public share.
    New-format links (redeem) use Badger auth + personal SharePoint API.
    Classic links use api.onedrive.com with authkey.
    """
    if access.redeem:
        token = _badger_token(opener)
        redeem = _validate_onedrive_token(access.redeem, kind="redeem")
        api = f"{_PERSONAL_SHARES_API}/u!{redeem}/driveitem"
        _assert_ms_onedrive_url(api, what="OneDrive API")
        req = Request(
            api,
            headers={
                "User-Agent": USER_AGENT,
                "Authorization": f"Badger {token}",
                "Prefer": "autoredeem",
                "Accept": "application/json",
            },
        )
    elif access.resid and access.container_id:
        cid = _validate_onedrive_token(access.container_id, kind="cid")
        resid = _validate_onedrive_token(access.resid, kind="resid")
        api = f"https://api.onedrive.com/v1.0/drives/{quote(cid, safe='')}/items/{quote(resid, safe='!')}"
        if access.auth_key:
            auth = _validate_onedrive_token(access.auth_key, kind="authkey")
            api += f"?authkey={quote(auth, safe='!_-')}"
        _assert_ms_onedrive_url(api, what="OneDrive API")
        req = Request(
            api,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/json",
            },
        )
    else:
        raise DownloadError(
            "无法解析 OneDrive 分享参数。请确认链接完整，或改用 Google Drive / 直接 https 链接。"
        )

    try:
        resp = opener.open(req, timeout=60)
        data = json.loads(resp.read().decode("utf-8", errors="replace"))
    except HTTPError as e:
        # Do not surface raw API bodies (may include tokens / internal fields) to UI.
        raise DownloadError(f"解析 OneDrive 文件信息失败: HTTP {e.code}") from e
    except Exception as e:
        raise DownloadError(f"解析 OneDrive 文件信息失败: {e}") from e

    if not isinstance(data, dict):
        raise DownloadError("OneDrive 返回了无效的文件信息")

    # Folder?
    if data.get("folder") is not None:
        raise DownloadError("这是文件夹分享链接，请改为分享单个 .zip / .7z 文件。")

    name = str(data.get("name") or "")
    dl = (
        data.get("@content.downloadUrl")
        or data.get("@microsoft.graph.downloadUrl")
        or ""
    )
    if not dl:
        raise DownloadError(
            "OneDrive 未返回下载地址。请确认链接是「知道链接的任何人可查看」的文件分享。"
        )
    _assert_ms_onedrive_url(str(dl), what="OneDrive 下载地址")
    return str(dl), name


def _is_blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    # Unwrap IPv4-mapped IPv6 (::ffff:127.0.0.1) before policy checks.
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped

    # Prefer is_global: also covers CGNAT 100.64.0.0/10, documentation nets, etc.
    # that are not always flagged by is_private alone.
    if not ip.is_global:
        return True
    if ip.is_private or ip.is_loopback or ip.is_link_local:
        return True
    if ip.is_reserved or ip.is_multicast or ip.is_unspecified:
        return True
    # Cloud metadata endpoint (also link-local / non-global, kept explicitly).
    if ip.version == 4 and str(ip) == "169.254.169.254":
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


def download_onedrive(url: str, dest_dir: Path, progress: ProgressCb | None = None) -> DownloadResult:
    """
    Download a public OneDrive / 1drv.ms share.

    New-format links (https://1drv.ms/u/c/...) no longer work via
    api.onedrive.com/shares/.../content (returns 401). We:
      1) follow the short link to onedrive.live.com / SharePoint
      2) for redeem-based links: obtain a Badger guest token and query
         my.microsoftpersonalcontent.com for @content.downloadUrl
      3) for classic authkey links: query api.onedrive.com drives API
      4) stream the resolved download URL
    """
    url = (url or "").strip()
    if not url:
        raise DownloadError("OneDrive 链接为空")

    opener, _jar = _opener(allow_http=False)
    host0 = (urlparse(url).hostname or "").lower()

    try:
        # SharePoint share page already in long form
        if host0.endswith("sharepoint.com") or host0.endswith("sharepoint.cn"):
            direct = _sharepoint_force_download_url(url)
            if not direct:
                raise DownloadError("无法识别该 SharePoint 分享链接格式")
            result = _download_with_opener(opener, direct, dest_dir, progress)
            return DownloadResult(result.path, url, result.filename, result.bytes_written)

        # CDN direct
        if host0.endswith(".1drv.com") and host0 != "1drv.ms":
            result = _download_with_opener(opener, url, dest_dir, progress)
            return DownloadResult(result.path, url, result.filename, result.bytes_written)

        expanded = url
        if host0 in {"1drv.ms", "onedrive.live.com"} or host0.endswith(".1drv.ms"):
            expanded = _follow_to_final_url(opener, url)

        exp_host = (urlparse(expanded).hostname or "").lower()
        if exp_host.endswith("sharepoint.com") or exp_host.endswith("sharepoint.cn"):
            direct = _sharepoint_force_download_url(expanded)
            if direct:
                result = _download_with_opener(opener, direct, dest_dir, progress)
                return DownloadResult(result.path, url, result.filename, result.bytes_written)

        access = _parse_onedrive_access(expanded)
        # If redeem missing but original was 1drv.ms/u/c, redeem is often only on redirect chain;
        # also try encoding original as last resort via driveitem after re-follow.
        if not access.redeem and not access.auth_key and host0 == "1drv.ms":
            # redeem sometimes only on intermediate Location; re-parse query from expanded
            # already done — also stash redeem from original short link encoding path
            pass

        if access.redeem or (access.resid and access.container_id):
            dl_url, suggested = _driveitem_download_url(opener, access)
            result = _download_with_opener(
                opener, dl_url, dest_dir, progress, preferred_name=suggested
            )
            return DownloadResult(result.path, url, result.filename, result.bytes_written)

        # Classic download endpoint fallback
        if access.resid:
            q = {"resid": access.resid}
            if access.auth_key:
                q["authkey"] = access.auth_key
            if access.container_id:
                q["cid"] = access.container_id
            fallback = "https://onedrive.live.com/download?" + urlencode(q)
            result = _download_with_opener(opener, fallback, dest_dir, progress)
            return DownloadResult(result.path, url, result.filename, result.bytes_written)

        raise DownloadError(
            "无法从该 OneDrive 链接解析下载地址。\n"
            "请确认：1) 分享的是单个文件；2) 权限为「知道链接的任何人」；"
            "3) 链接完整未截断。也可改用 Google Drive。"
        )
    except DownloadError:
        raise
    except Exception as e:
        raise DownloadError(f"OneDrive 下载失败: {e}") from e


def _download_with_opener(
    opener,
    url: str,
    dest_dir: Path,
    progress: ProgressCb | None = None,
    preferred_name: str = "",
    *,
    require_ms_host: bool = True,
) -> DownloadResult:
    if require_ms_host:
        _assert_ms_onedrive_url(url, what="下载")
    else:
        assert_url_safe(url, allow_http=False)
    try:
        resp = opener.open(_request(url), timeout=120)
    except HTTPError as e:
        raise DownloadError(f"下载失败: HTTP {e.code} {e.reason}") from e
    except (URLError, DownloadError) as e:
        raise DownloadError(f"下载失败: {e}") from e

    final_url = resp.geturl()
    if require_ms_host:
        _assert_ms_onedrive_url(final_url, what="下载重定向")
    else:
        assert_url_safe(final_url, allow_http=False)
    cd = resp.headers.get("Content-Disposition")
    filename = (
        _filename_from_cd(cd)
        or _safe_basename(preferred_name)
        or _filename_from_url(final_url)
        or _filename_from_url(url)
    )
    tmp = dest_dir / f"dl_{uuid.uuid4().hex}.part"
    written, head = _stream_to_file(resp, tmp, progress)

    if written < 512 * 1024:
        sample = tmp.read_bytes()[:1024]
        if _is_html_bytes(sample):
            tmp.unlink(missing_ok=True)
            raise DownloadError(
                "下载到的是网页，不是压缩包。"
                "请使用直接下载链接、Google Drive 或 OneDrive 文件分享链接。"
            )

    final_name = _ensure_archive_name(filename, head)
    final_path = dest_dir / final_name
    if final_path.exists():
        final_path.unlink()
    tmp.replace(final_path)
    return DownloadResult(final_path, final_url, final_name, written)


def download_http(url: str, dest_dir: Path, progress: ProgressCb | None = None) -> DownloadResult:
    assert_url_safe(url, allow_http=False)
    opener, _ = _opener(allow_http=False)
    try:
        resp = opener.open(_request(url), timeout=120)
    except HTTPError as e:
        raise DownloadError(f"下载失败: HTTP {e.code} {e.reason}") from e
    except (URLError, DownloadError) as e:
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
                "下载到的是网页，不是压缩包。"
                "请使用直接下载链接、Google Drive 或 OneDrive 文件分享链接。"
            )

    final_name = _ensure_archive_name(filename, head)
    final_path = dest_dir / final_name
    if final_path.exists():
        final_path.unlink()
    tmp.replace(final_path)
    return DownloadResult(final_path, final_url, final_name, written)


def download_archive(url: str, progress: ProgressCb | None = None) -> DownloadResult:
    """
    Download an archive from Google Drive, OneDrive, or a direct https link.
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

    if is_onedrive_url(url):
        return download_onedrive(url, dest_dir, progress)

    assert_url_safe(url, allow_http=False)
    return download_http(url, dest_dir, progress)
