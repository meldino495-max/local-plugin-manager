from __future__ import annotations

import json
import logging
import shutil
import sys
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_data_dir_override: Path | None = None


def _is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def project_root() -> Path:
    """
    Writable app root (config / data next to the program).
    When frozen (PyInstaller), this is the folder containing the .exe.
    """
    if _is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


def bundle_root() -> Path:
    """
    Read-only bundled assets root.
    When frozen, PyInstaller extracts to sys._MEIPASS.
    """
    if _is_frozen():
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            return Path(meipass)
        return Path(sys.executable).resolve().parent
    return project_root()


def resources_dir() -> Path:
    return bundle_root() / "resources"


def app_icon_path() -> Path:
    """Prefer .ico on Windows, fall back to .png/.jpg."""
    res = resources_dir()
    for name in ("icon.ico", "icon.png", "icon.jpg"):
        p = res / name
        if p.is_file():
            return p
    return res / "icon.png"


def default_data_dir() -> Path:
    return project_root() / "data"


def config_path() -> Path:
    """App settings that stay next to the program (not inside cache)."""
    return project_root() / "app_config.json"


def load_app_config() -> dict[str, Any]:
    path = config_path()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        log.exception("Failed to read app_config.json")
        return {}


def save_app_config(cfg: dict[str, Any]) -> None:
    path = config_path()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def get_configured_data_dir() -> Path:
    cfg = load_app_config()
    raw = (cfg.get("data_dir") or "").strip()
    if not raw:
        return default_data_dir()
    try:
        return Path(raw).expanduser().resolve()
    except OSError:
        return Path(raw).expanduser()


def data_dir() -> Path:
    global _data_dir_override
    d = _data_dir_override if _data_dir_override is not None else get_configured_data_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d


def set_data_dir_runtime(path: Path | None) -> None:
    """Update in-memory data dir (after config change in the same session)."""
    global _data_dir_override
    _data_dir_override = path.resolve() if path is not None else None


def store_path() -> Path:
    return data_dir() / "store.json"


def versions_root() -> Path:
    d = data_dir() / "versions"
    d.mkdir(parents=True, exist_ok=True)
    return d


def archives_root() -> Path:
    d = data_dir() / "archives"
    d.mkdir(parents=True, exist_ok=True)
    return d


def temp_root() -> Path:
    d = data_dir() / "temp"
    d.mkdir(parents=True, exist_ok=True)
    return d


def logs_root() -> Path:
    d = data_dir() / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def ensure_runtime_dirs() -> None:
    data_dir()
    versions_root()
    archives_root()
    temp_root()
    logs_root()


def is_default_data_dir(path: Path | None = None) -> bool:
    current = (path or data_dir()).resolve()
    return current == default_data_dir().resolve()


def _rewrite_store_paths(store_file: Path, old_root: Path, new_root: Path) -> None:
    if not store_file.is_file():
        return
    try:
        raw = json.loads(store_file.read_text(encoding="utf-8"))
    except Exception:
        return

    old_s = str(old_root.resolve())
    new_s = str(new_root.resolve())
    # Also try casefold replacement keys on Windows by doing replace on both forms
    changed = False

    def rewrite(value: str) -> str:
        nonlocal changed
        if not value:
            return value
        try:
            p = Path(value)
            # If under old_root, relocate
            try:
                rel = p.resolve().relative_to(old_root.resolve())
                out = str((new_root / rel).resolve())
                if out != value:
                    changed = True
                return out
            except ValueError:
                pass
        except OSError:
            pass
        if old_s in value:
            changed = True
            return value.replace(old_s, new_s)
        # case-insensitive drive path replace
        if old_s.casefold() in value.casefold():
            # crude: replace ignoring case via regex-like scan
            idx = value.casefold().find(old_s.casefold())
            if idx >= 0:
                changed = True
                return value[:idx] + new_s + value[idx + len(old_s) :]
        return value

    meta = raw.get("meta") or {}
    for _uid, m in meta.items():
        if not isinstance(m, dict):
            continue
        for h in m.get("history") or []:
            if not isinstance(h, dict):
                continue
            if "archive_path" in h:
                h["archive_path"] = rewrite(str(h.get("archive_path") or ""))
            if "snapshot_path" in h:
                h["snapshot_path"] = rewrite(str(h.get("snapshot_path") or ""))

    if changed:
        tmp = store_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(store_file)


def migrate_data_dir(old_root: Path, new_root: Path) -> tuple[bool, str]:
    """
    Copy cache data from old_root to new_root and rewrite absolute paths in store.json.
    """
    old_root = old_root.resolve()
    new_root = new_root.resolve()
    if old_root == new_root:
        return True, "目录未变化"

    if not old_root.exists():
        new_root.mkdir(parents=True, exist_ok=True)
        return True, "原目录不存在，已使用新目录"

    try:
        new_root.mkdir(parents=True, exist_ok=True)
        for name in ("store.json", "versions", "archives", "logs"):
            src = old_root / name
            dst = new_root / name
            if not src.exists():
                continue
            if src.is_file():
                shutil.copy2(src, dst)
            elif src.is_dir():
                if dst.exists():
                    # merge copy
                    for item in src.rglob("*"):
                        rel = item.relative_to(src)
                        target = dst / rel
                        if item.is_dir():
                            target.mkdir(parents=True, exist_ok=True)
                        else:
                            target.parent.mkdir(parents=True, exist_ok=True)
                            shutil.copy2(item, target)
                else:
                    shutil.copytree(src, dst)

        _rewrite_store_paths(new_root / "store.json", old_root, new_root)
        (new_root / "temp").mkdir(parents=True, exist_ok=True)
        return True, f"已迁移到：\n{new_root}"
    except Exception as e:
        log.exception("migrate_data_dir failed")
        return False, f"迁移失败: {e}"


def set_configured_data_dir(new_dir: Path, migrate: bool = True) -> tuple[bool, str]:
    """
    Persist new cache directory. Optionally migrate existing data.
    """
    new_dir = new_dir.expanduser().resolve()
    old_dir = data_dir().resolve()

    try:
        new_dir.mkdir(parents=True, exist_ok=True)
        # write probe
        probe = new_dir / ".bem_write_test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
    except Exception as e:
        return False, f"无法使用该目录（无权限或路径无效）: {e}"

    if migrate and old_dir != new_dir and old_dir.exists():
        ok, msg = migrate_data_dir(old_dir, new_dir)
        if not ok:
            return False, msg
    else:
        msg = f"已切换缓存目录为：\n{new_dir}"
        if not migrate and old_dir != new_dir:
            msg += "\n（未迁移旧数据；历史/压缩包仍在原目录）"

    cfg = load_app_config()
    if new_dir.resolve() == default_data_dir().resolve():
        cfg.pop("data_dir", None)
    else:
        cfg["data_dir"] = str(new_dir)
    save_app_config(cfg)
    set_data_dir_runtime(new_dir)
    ensure_runtime_dirs()
    return True, msg
