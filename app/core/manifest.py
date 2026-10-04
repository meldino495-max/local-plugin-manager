from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


_MSG_RE = re.compile(r"^__MSG_([A-Za-z0-9_]+)__$")


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        text = path.read_text(encoding="utf-8-sig")
        data = json.loads(text)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _locale_message(ext_dir: Path, default_locale: str, key: str) -> str | None:
    if not key:
        return None
    # Chromium resolves message keys case-insensitively.
    locales: list[str] = []
    for loc in (default_locale, "en", "en_US", "en_GB"):
        if loc and loc not in locales:
            locales.append(loc)
    for loc in locales:
        msg_file = ext_dir / "_locales" / loc / "messages.json"
        data = _read_json(msg_file)
        if not data:
            continue
        entry = data.get(key)
        if entry is None:
            key_l = key.lower()
            for k, v in data.items():
                if isinstance(k, str) and k.lower() == key_l:
                    entry = v
                    break
        if isinstance(entry, dict):
            msg = entry.get("message")
            if msg is not None:
                return str(msg)
    return None


def resolve_i18n(value: str, ext_dir: Path, default_locale: str) -> str:
    if not isinstance(value, str):
        return str(value or "")
    m = _MSG_RE.match(value.strip())
    if not m:
        return value
    resolved = _locale_message(ext_dir, default_locale, m.group(1))
    return resolved if resolved else value


def parse_manifest(ext_dir: Path) -> dict[str, Any] | None:
    """Parse Chromium/Firefox-style manifest.json inside an extension folder."""
    manifest_path = ext_dir / "manifest.json"
    data = _read_json(manifest_path)
    if not data:
        return None

    locale = str(data.get("default_locale") or "")
    name = resolve_i18n(str(data.get("name") or ext_dir.name), ext_dir, locale)
    desc = resolve_i18n(str(data.get("description") or ""), ext_dir, locale)
    version = str(data.get("version") or "")
    mv = data.get("manifest_version")
    try:
        manifest_version = int(mv) if mv is not None else 0
    except (TypeError, ValueError):
        manifest_version = 0

    return {
        "name": name,
        "description": desc,
        "version": version,
        "manifest_version": manifest_version,
        "update_url": str(data.get("update_url") or ""),
        "raw": data,
    }


def resolve_icon_path(ext_dir: Path, manifest: dict[str, Any] | None) -> str:
    """Pick the best icon file path from a Chromium/Firefox manifest."""
    if not manifest:
        return ""
    raw = manifest.get("raw") if "raw" in manifest else manifest
    if not isinstance(raw, dict):
        return ""

    candidates: list[str] = []
    icons = raw.get("icons")
    if isinstance(icons, dict):
        sized: list[tuple[int, str]] = []
        for k, v in icons.items():
            try:
                sized.append((int(k), str(v)))
            except (TypeError, ValueError):
                if v:
                    candidates.append(str(v))
        sized.sort(key=lambda x: x[0], reverse=True)
        candidates.extend(p for _, p in sized)

    for key in ("action", "browser_action", "page_action"):
        block = raw.get(key)
        if not isinstance(block, dict):
            continue
        default_icon = block.get("default_icon")
        if isinstance(default_icon, str):
            candidates.append(default_icon)
        elif isinstance(default_icon, dict):
            sized = []
            for k, v in default_icon.items():
                try:
                    sized.append((int(k), str(v)))
                except (TypeError, ValueError):
                    if v:
                        candidates.append(str(v))
            sized.sort(key=lambda x: x[0], reverse=True)
            candidates.extend(p for _, p in sized)

    seen: set[str] = set()
    for rel in candidates:
        rel = rel.replace("\\", "/").lstrip("/")
        if not rel or rel in seen:
            continue
        seen.add(rel)
        path = ext_dir / rel
        if path.is_file():
            return str(path.resolve())
    return ""


def find_extension_root(extracted: Path) -> Path | None:
    """
    Locate the real extension root after archive extraction.
    Accepts:
      - flat layout with manifest.json at root
      - single top-level folder containing manifest.json
      - nested single-folder wrappers (up to 3 levels)
    """
    extracted = extracted.resolve()
    if (extracted / "manifest.json").is_file():
        return extracted

    current = extracted
    for _ in range(3):
        children = [p for p in current.iterdir() if not p.name.startswith(".")]
        dirs = [p for p in children if p.is_dir()]
        files = [p for p in children if p.is_file()]
        # Ignore macOS junk
        dirs = [d for d in dirs if d.name != "__MACOSX"]
        if (current / "manifest.json").is_file():
            return current
        if len(dirs) == 1 and not any(f.name == "manifest.json" for f in files):
            current = dirs[0]
            continue
        # Multiple items: prefer a dir that has manifest.json
        for d in dirs:
            if (d / "manifest.json").is_file():
                return d
        break

    # Deep search (bounded)
    for p in extracted.rglob("manifest.json"):
        if "__MACOSX" in p.parts:
            continue
        return p.parent
    return None
