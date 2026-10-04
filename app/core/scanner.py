from __future__ import annotations

import json
import logging
import os
from dataclasses import replace
from pathlib import Path
from typing import Any

from app.core.manifest import parse_manifest, resolve_icon_path
from app.models.extension import BrowserKind, ExtensionInfo

log = logging.getLogger(__name__)

# Chromium ExtensionLocation
_LOC_INTERNAL = 1          # Chrome/Edge Web Store
_LOC_UNPACKED = 4          # Load unpacked / local
_LOC_COMPONENT = 5         # Built-in browser component

_STORE_UPDATE_MARKERS = (
    "clients2.google.com",
    "clients2.googleusercontent.com",
    "chrome.google.com/webstore",
    "edge.microsoft.com",
    "extensionwebstorebase",
    "microsoftedge.microsoft.com",
)


def _local_appdata() -> Path:
    return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")


def _roaming_appdata() -> Path:
    return Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")


def _chromium_roots() -> list[tuple[BrowserKind, Path]]:
    la = _local_appdata()
    return [
        (BrowserKind.CHROME, la / "Google" / "Chrome" / "User Data"),
        (BrowserKind.EDGE, la / "Microsoft" / "Edge" / "User Data"),
        (BrowserKind.BRAVE, la / "BraveSoftware" / "Brave-Browser" / "User Data"),
    ]


def _is_profile_dir(name: str) -> bool:
    if name in {"Default", "Guest Profile", "System Profile"}:
        return True
    if name.startswith("Profile "):
        return True
    return False


def _pick_latest_version_dir(ext_id_dir: Path) -> Path | None:
    versions = [p for p in ext_id_dir.iterdir() if p.is_dir() and not p.name.startswith(".")]
    if not versions:
        return None

    def sort_key(p: Path) -> tuple:
        name = p.name
        base, _, suffix = name.partition("_")
        parts: list[int] = []
        for chunk in base.split("."):
            try:
                parts.append(int(chunk))
            except ValueError:
                parts.append(-1)
        try:
            suf = int(suffix) if suffix else 0
        except ValueError:
            suf = 0
        return (tuple(parts), suf, name)

    versions.sort(key=sort_key)
    return versions[-1]


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _load_extension_prefs(profile_dir: Path) -> dict[str, dict[str, Any]]:
    """Read extensions.settings from Secure Preferences / Preferences."""
    out: dict[str, dict[str, Any]] = {}
    for name in ("Secure Preferences", "Preferences"):
        data = _read_json(profile_dir / name)
        if not data:
            continue
        settings = ((data.get("extensions") or {}).get("settings") or {})
        if not isinstance(settings, dict):
            continue
        for eid, info in settings.items():
            if isinstance(info, dict) and eid not in out:
                out[eid] = info
    return out


def _update_url_is_store(update_url: str) -> bool:
    u = (update_url or "").lower()
    return any(m in u for m in _STORE_UPDATE_MARKERS)


def _classify_install(
    pref: dict[str, Any] | None,
    update_url: str,
) -> tuple[str, bool, bool]:
    """
    Returns (install_source, is_webstore, is_component).
    Prefer Chromium prefs location / from_webstore; fall back to update_url.
    """
    if pref:
        loc = pref.get("location")
        try:
            loc_i = int(loc) if loc is not None else None
        except (TypeError, ValueError):
            loc_i = None

        if loc_i == _LOC_COMPONENT:
            return "component", False, True
        if pref.get("from_webstore") is True or loc_i == _LOC_INTERNAL:
            return "webstore", True, False
        if loc_i == _LOC_UNPACKED:
            return "local", False, False

    if _update_url_is_store(update_url):
        return "webstore", True, False

    # No store update URL and no component marker → treat as local/unpacked
    return "local", False, False


def _resolve_pref_path(profile_dir: Path, raw_path: str) -> Path | None:
    """Unpacked extensions store absolute or profile-relative paths in prefs."""
    if not raw_path:
        return None
    p = Path(raw_path)
    if not p.is_absolute():
        p = profile_dir / raw_path
    try:
        p = p.resolve()
    except OSError:
        return None
    return p if p.is_dir() else None


def _make_ext_info(
    *,
    browser: BrowserKind,
    profile: str,
    ext_id: str,
    version_dir: Path,
    pref: dict[str, Any] | None,
) -> ExtensionInfo | None:
    info = parse_manifest(version_dir)
    if not info:
        return None
    source, is_webstore, is_component = _classify_install(
        pref, str(info.get("update_url") or "")
    )
    # Explicit unpacked path from prefs always counts as local.
    if pref is not None:
        try:
            loc_i = int(pref.get("location")) if pref.get("location") is not None else None
        except (TypeError, ValueError):
            loc_i = None
        if loc_i == _LOC_UNPACKED:
            source, is_webstore, is_component = "local", False, False

    uid = f"{browser.value}|{profile}|{ext_id}"
    return ExtensionInfo(
        uid=uid,
        browser=browser,
        profile=profile,
        ext_id=ext_id,
        name=info["name"] or ext_id,
        version=info["version"] or version_dir.name,
        description=info.get("description") or "",
        path=str(version_dir.resolve()),
        manifest_version=info.get("manifest_version") or 0,
        icon_path=resolve_icon_path(version_dir, info),
        is_webstore=is_webstore,
        is_component=is_component,
        install_source=source,
    )


def _scan_chromium(browser: BrowserKind, user_data: Path) -> list[ExtensionInfo]:
    results: list[ExtensionInfo] = []
    if not user_data.is_dir():
        return results

    try:
        profiles = [p for p in user_data.iterdir() if p.is_dir() and _is_profile_dir(p.name)]
    except OSError as e:
        log.warning("Cannot list %s: %s", user_data, e)
        return results

    for profile in profiles:
        prefs = _load_extension_prefs(profile)
        seen: set[str] = set()

        # 1) Load-unpacked / local extensions (path is outside Extensions/)
        for ext_id, pref in prefs.items():
            try:
                loc_i = int(pref.get("location")) if pref.get("location") is not None else None
            except (TypeError, ValueError):
                loc_i = None
            if loc_i != _LOC_UNPACKED:
                continue
            version_dir = _resolve_pref_path(profile, str(pref.get("path") or ""))
            if version_dir is None:
                continue
            item = _make_ext_info(
                browser=browser,
                profile=profile.name,
                ext_id=ext_id,
                version_dir=version_dir,
                pref=pref,
            )
            if item is None:
                continue
            results.append(item)
            seen.add(ext_id)

        # 2) Installed under profile/Extensions (usually Web Store / sideload)
        ext_root = profile / "Extensions"
        if not ext_root.is_dir():
            continue
        try:
            ext_ids = [p for p in ext_root.iterdir() if p.is_dir()]
        except OSError:
            continue
        for ext_dir in ext_ids:
            if ext_dir.name.startswith("Temp") or ext_dir.name in seen:
                continue
            version_dir = _pick_latest_version_dir(ext_dir)
            if version_dir is None:
                continue
            item = _make_ext_info(
                browser=browser,
                profile=profile.name,
                ext_id=ext_dir.name,
                version_dir=version_dir,
                pref=prefs.get(ext_dir.name),
            )
            if item is None:
                continue
            results.append(item)
            seen.add(ext_dir.name)
    return results


def _scan_firefox() -> list[ExtensionInfo]:
    results: list[ExtensionInfo] = []
    profiles_root = _roaming_appdata() / "Mozilla" / "Firefox" / "Profiles"
    if not profiles_root.is_dir():
        return results

    try:
        profiles = [p for p in profiles_root.iterdir() if p.is_dir()]
    except OSError:
        return results

    for profile in profiles:
        for candidate in (profile / "extensions", profile / "browser-extension-data"):
            if not candidate.is_dir():
                continue
            try:
                children = list(candidate.iterdir())
            except OSError:
                continue
            for child in children:
                target: Path | None = None
                if child.is_dir() and (child / "manifest.json").is_file():
                    target = child
                elif child.is_file() and child.suffix.lower() == ".xpi":
                    uid = f"firefox|{profile.name}|{child.name}"
                    results.append(
                        ExtensionInfo(
                            uid=uid,
                            browser=BrowserKind.FIREFOX,
                            profile=profile.name,
                            ext_id=child.stem,
                            name=child.stem,
                            version="",
                            description="打包的 .xpi（请先解压为文件夹后再用本工具管理）",
                            path=str(child.resolve()),
                            manifest_version=0,
                            install_source="unknown",
                        )
                    )
                    continue
                if target is None:
                    continue
                info = parse_manifest(target)
                if not info:
                    continue
                update_url = str(info.get("update_url") or "")
                source, is_webstore, is_component = _classify_install(None, update_url)
                # Firefox AMO often uses addons.mozilla.org
                if "addons.mozilla.org" in update_url.lower():
                    source, is_webstore, is_component = "webstore", True, False
                uid = f"firefox|{profile.name}|{target.name}"
                results.append(
                    ExtensionInfo(
                        uid=uid,
                        browser=BrowserKind.FIREFOX,
                        profile=profile.name,
                        ext_id=target.name,
                        name=info["name"] or target.name,
                        version=info["version"] or "",
                        description=info.get("description") or "",
                        path=str(target.resolve()),
                        manifest_version=info.get("manifest_version") or 0,
                        icon_path=resolve_icon_path(target, info),
                        is_webstore=is_webstore,
                        is_component=is_component,
                        install_source=source,
                    )
                )
    return results


def _norm_path_key(path: str) -> str:
    """Case-insensitive absolute path key for Windows dedupe."""
    try:
        return str(Path(path).resolve()).casefold()
    except OSError:
        return path.replace("\\", "/").rstrip("/").casefold()


def dedupe_by_path(items: list[ExtensionInfo]) -> list[ExtensionInfo]:
    """
    Same plugin folder loaded in multiple browsers/profiles → one row.
    Prefer local + has icon; merge location labels into profile/locations.
    """
    groups: dict[str, list[ExtensionInfo]] = {}
    for item in items:
        if not item.path:
            groups.setdefault(f"empty|{item.uid}", []).append(item)
            continue
        groups.setdefault(_norm_path_key(item.path), []).append(item)

    out: list[ExtensionInfo] = []
    for key, group in groups.items():
        group.sort(
            key=lambda e: (
                0 if e.install_source == "local" else 1,
                0 if e.icon_path else 1,
                e.browser.value,
                e.profile,
                e.name.lower(),
            )
        )
        primary = group[0]
        if len(group) == 1:
            loc = f"{primary.browser.label}/{primary.profile}"
            out.append(
                replace(
                    primary,
                    uid=f"path|{key}" if not key.startswith("empty|") else primary.uid,
                    locations=loc,
                    profile=loc,
                )
            )
            continue

        loc_labels: list[str] = []
        seen_loc: set[str] = set()
        browsers: list[str] = []
        seen_browser: set[str] = set()
        for e in group:
            loc = f"{e.browser.label}/{e.profile}"
            if loc not in seen_loc:
                seen_loc.add(loc)
                loc_labels.append(loc)
            if e.browser.label not in seen_browser:
                seen_browser.add(e.browser.label)
                browsers.append(e.browser.label)

        merged_profile = " · ".join(loc_labels)
        # Keep primary browser enum; UI shows merged profile string.
        out.append(
            replace(
                primary,
                uid=f"path|{key}",
                locations=merged_profile,
                profile=merged_profile,
                description=(
                    (primary.description + "\n" if primary.description else "")
                    + f"同路径共 {len(group)} 处加载：{merged_profile}"
                ).strip(),
            )
        )

    out.sort(key=lambda e: (e.install_source != "local", e.name.lower(), e.path.lower()))
    return out


def scan_all_extensions() -> list[ExtensionInfo]:
    found: list[ExtensionInfo] = []
    for browser, root in _chromium_roots():
        found.extend(_scan_chromium(browser, root))
    found.extend(_scan_firefox())
    return dedupe_by_path(found)
