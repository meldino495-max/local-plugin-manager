from __future__ import annotations

import json
import logging
from typing import Any

from app.models.extension import ExtMeta, VersionRecord
from app.utils.paths import store_path

log = logging.getLogger(__name__)

DEFAULT_CATEGORIES = ["未分类", "广告拦截", "工具", "开发", "隐私", "其他"]


class AppStore:
    """Persistent pins, categories, and version history."""

    def __init__(self) -> None:
        self.categories: list[str] = list(DEFAULT_CATEGORIES)
        self.meta: dict[str, ExtMeta] = {}
        # Default: hide Chrome/Edge Web Store extensions (show local/unpacked).
        self.hide_webstore: bool = True
        self.hide_component: bool = True
        # Plugins that only exist via imported history bundles.
        self.imported_plugins: list[dict[str, Any]] = []
        self.load()

    def load(self) -> None:
        path = store_path()
        if not path.is_file():
            return
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            log.exception("Failed to load store.json")
            return
        cats = raw.get("categories") or []
        merged = list(DEFAULT_CATEGORIES)
        for c in cats:
            if c and c not in merged:
                merged.append(c)
        self.categories = merged
        self.meta = {
            uid: ExtMeta.from_dict(data)
            for uid, data in (raw.get("meta") or {}).items()
        }
        prefs = raw.get("prefs") or {}
        if "hide_webstore" in prefs:
            self.hide_webstore = bool(prefs.get("hide_webstore"))
        if "hide_component" in prefs:
            self.hide_component = bool(prefs.get("hide_component"))
        self.imported_plugins = list(raw.get("imported_plugins") or [])

    def save(self) -> None:
        payload: dict[str, Any] = {
            "categories": self.categories,
            "prefs": {
                "hide_webstore": self.hide_webstore,
                "hide_component": self.hide_component,
            },
            "imported_plugins": self.imported_plugins,
            "meta": {uid: m.to_dict() for uid, m in self.meta.items()},
        }
        path = store_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)

    def get_meta(self, uid: str) -> ExtMeta:
        if uid not in self.meta:
            self.meta[uid] = ExtMeta()
        return self.meta[uid]

    def set_pinned(self, uid: str, pinned: bool) -> None:
        self.get_meta(uid).pinned = pinned
        self.save()

    def set_category(self, uid: str, category: str) -> None:
        category = (category or "未分类").strip() or "未分类"
        if category not in self.categories:
            self.categories.append(category)
        self.get_meta(uid).category = category
        self.save()

    def add_category(self, category: str) -> None:
        category = category.strip()
        if category and category not in self.categories:
            self.categories.append(category)
            self.save()

    def add_version(self, uid: str, record: VersionRecord, mark_latest: bool = True) -> None:
        meta = self.get_meta(uid)
        if mark_latest:
            for h in meta.history:
                h.is_latest = False
            record.is_latest = True
        meta.history.append(record)
        # Keep newest last; UI may reverse for display
        self.save()

    def mark_latest(self, uid: str, record_id: str) -> None:
        meta = self.get_meta(uid)
        for h in meta.history:
            h.is_latest = h.id == record_id
        self.save()

    def get_history(self, uid: str) -> list[VersionRecord]:
        return list(self.get_meta(uid).history)

    def find_record(self, uid: str, record_id: str) -> VersionRecord | None:
        for h in self.get_meta(uid).history:
            if h.id == record_id:
                return h
        return None

    def latest_record(self, uid: str) -> VersionRecord | None:
        hist = self.get_history(uid)
        for h in reversed(hist):
            if h.is_latest:
                return h
        return hist[-1] if hist else None

    def set_hide_webstore(self, hide: bool) -> None:
        self.hide_webstore = bool(hide)
        self.save()

    def add_imported_plugins(self, items: list[dict[str, Any]]) -> None:
        existing = {d.get("uid") for d in self.imported_plugins}
        for item in items:
            uid = item.get("uid")
            if not uid:
                continue
            if uid in existing:
                # refresh fields
                self.imported_plugins = [
                    item if d.get("uid") == uid else d for d in self.imported_plugins
                ]
            else:
                self.imported_plugins.append(item)
                existing.add(uid)
        self.save()
