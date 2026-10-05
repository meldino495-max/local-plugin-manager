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


_JUNK_DIR_NAMES = {
    "__macosx",
    ".git",
    ".svn",
    ".hg",
    "node_modules",
    ".idea",
    ".vscode",
    "__pycache__",
}


def _is_junk_dir(name: str) -> bool:
    return name.lower() in _JUNK_DIR_NAMES or name.startswith(".")


def _manifest_looks_valid(manifest_path: Path) -> bool:
    """True if file parses as a Chromium/Firefox extension manifest."""
    data = _read_json(manifest_path)
    if not data:
        return False
    # Prefer real extension manifests; still accept if name/version present.
    if "manifest_version" in data:
        return True
    if data.get("name") and data.get("version"):
        return True
    return False


def _candidate_score(root: Path, extract_base: Path) -> tuple[int, int, str]:
    """
    Lower is better:
      - prefer valid extension manifest
      - prefer shallower path under extract_base
      - stable tie-break by path string
    """
    try:
        rel = root.resolve().relative_to(extract_base.resolve())
        depth = len(rel.parts)
        rel_s = rel.as_posix()
    except ValueError:
        depth = 99
        rel_s = str(root)
    valid = 0 if _manifest_looks_valid(root / "manifest.json") else 1
    return (valid, depth, rel_s.lower())


def find_extension_root(extracted: Path) -> Path | None:
    """
    Locate the real extension root after archive extraction.

    Handles common pack layouts:
      - flat: manifest.json at extract root
      - one wrapper folder (or several nested single folders) then manifest
      - wrapper + readme/license junk beside the real folder
      - deep search for the shallowest valid manifest.json
    """
    extracted = extracted.resolve()
    if not extracted.is_dir():
        return None

    if (extracted / "manifest.json").is_file() and _manifest_looks_valid(
        extracted / "manifest.json"
    ):
        return extracted

    # Unwrap nested single-directory wrappers (GitHub zip / 网盘打包常见).
    current = extracted
    for _ in range(8):
        try:
            children = [p for p in current.iterdir() if not _is_junk_dir(p.name)]
        except OSError:
            break
        dirs = [p for p in children if p.is_dir() and not _is_junk_dir(p.name)]
        files = [p for p in children if p.is_file()]
        if (current / "manifest.json").is_file():
            return current
        # Only one real folder and no sibling manifest → enter it
        if len(dirs) == 1 and not any(f.name.lower() == "manifest.json" for f in files):
            current = dirs[0]
            continue
        # Multiple siblings: if exactly one child dir has manifest.json, use it
        hits = [d for d in dirs if (d / "manifest.json").is_file()]
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            return min(hits, key=lambda d: _candidate_score(d, extracted))
        break

    # Bounded deep search — pick shallowest valid manifest (skip junk paths)
    valid: list[Path] = []
    weak: list[Path] = []
    try:
        for p in extracted.rglob("manifest.json"):
            if not p.is_file():
                continue
            if any(_is_junk_dir(part) for part in p.parts):
                continue
            parent = p.parent
            if _manifest_looks_valid(p):
                valid.append(parent)
            else:
                weak.append(parent)
    except OSError:
        return None

    pool = valid or weak
    if not pool:
        return None
    return min(pool, key=lambda d: _candidate_score(d, extracted))


def describe_extension_root(extracted: Path, root: Path) -> str:
    """Human-readable path of extension root relative to extract dir ('' if same)."""
    try:
        rel = root.resolve().relative_to(extracted.resolve())
        return "" if rel == Path(".") else rel.as_posix()
    except ValueError:
        return str(root)
