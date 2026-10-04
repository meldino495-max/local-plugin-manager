from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class BrowserKind(str, Enum):
    CHROME = "chrome"
    EDGE = "edge"
    BRAVE = "brave"
    FIREFOX = "firefox"
    UNKNOWN = "unknown"

    @property
    def label(self) -> str:
        return {
            BrowserKind.CHROME: "Chrome",
            BrowserKind.EDGE: "Edge",
            BrowserKind.BRAVE: "Brave",
            BrowserKind.FIREFOX: "Firefox",
            BrowserKind.UNKNOWN: "Unknown",
        }[self]


@dataclass
class ExtensionInfo:
    """A discovered installed browser extension."""

    uid: str  # stable id: browser|profile|ext_id
    browser: BrowserKind
    profile: str
    ext_id: str
    name: str
    version: str
    description: str
    path: str
    manifest_version: int = 0
    icon_path: str = ""
    is_webstore: bool = False
    is_component: bool = False
    install_source: str = "local"  # local | webstore | component | unknown
    # When the same folder is loaded in multiple browsers/profiles
    locations: str = ""  # e.g. "Chrome/Default · Brave/Profile 1"

    @property
    def source_label(self) -> str:
        return {
            "local": "本地",
            "webstore": "商店",
            "component": "系统",
            "imported": "导入",
            "unknown": "未知",
        }.get(self.install_source, self.install_source)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["browser"] = self.browser.value
        return d

    @staticmethod
    def from_dict(d: dict[str, Any]) -> ExtensionInfo:
        return ExtensionInfo(
            uid=d["uid"],
            browser=BrowserKind(d.get("browser", "unknown")),
            profile=d.get("profile", ""),
            ext_id=d.get("ext_id", ""),
            name=d.get("name", ""),
            version=d.get("version", ""),
            description=d.get("description", ""),
            path=d.get("path", ""),
            manifest_version=int(d.get("manifest_version") or 0),
            icon_path=d.get("icon_path") or "",
            is_webstore=bool(d.get("is_webstore", False)),
            is_component=bool(d.get("is_component", False)),
            install_source=d.get("install_source") or "local",
            locations=d.get("locations") or "",
        )


@dataclass
class VersionRecord:
    """One remembered package/snapshot for an extension."""

    id: str
    version: str
    label: str
    created_at: str
    source: str  # "upload" | "snapshot" | "rollback"
    archive_path: str = ""
    snapshot_path: str = ""
    note: str = ""
    is_latest: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict[str, Any]) -> VersionRecord:
        return VersionRecord(
            id=d["id"],
            version=d.get("version", ""),
            label=d.get("label", ""),
            created_at=d.get("created_at", ""),
            source=d.get("source", "upload"),
            archive_path=d.get("archive_path", ""),
            snapshot_path=d.get("snapshot_path", ""),
            note=d.get("note", ""),
            is_latest=bool(d.get("is_latest", False)),
        )


@dataclass
class ExtMeta:
    """User metadata for one extension uid."""

    pinned: bool = False
    category: str = "未分类"
    notes: str = ""
    history: list[VersionRecord] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "pinned": self.pinned,
            "category": self.category,
            "notes": self.notes,
            "history": [h.to_dict() for h in self.history],
        }

    @staticmethod
    def from_dict(d: dict[str, Any] | None) -> ExtMeta:
        if not d:
            return ExtMeta()
        return ExtMeta(
            pinned=bool(d.get("pinned", False)),
            category=d.get("category") or "未分类",
            notes=d.get("notes") or "",
            history=[VersionRecord.from_dict(x) for x in d.get("history") or []],
        )
