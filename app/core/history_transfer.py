from __future__ import annotations

import json
import logging
import re
import shutil
import uuid
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from app.core.archive import ArchiveError, create_zip, extract_archive
from app.models.extension import BrowserKind, ExtensionInfo, VersionRecord
from app.models.store import AppStore
from app.utils.paths import archives_root, temp_root

log = logging.getLogger(__name__)

FORMAT_ID = "bem-history-v1"


@dataclass
class TransferItemPreview:
    uid: str
    name: str
    version: str
    path: str
    history_count: int
    package_count: int  # records that have archive or snapshot to pack


@dataclass
class TransferResult:
    ok: bool
    message: str
    matched: int = 0
    orphaned: int = 0
    records: int = 0
    packages: int = 0


def _safe_seg(text: str) -> str:
    text = re.sub(r"[^\w.\-]+", "_", text, flags=re.UNICODE)
    return (text[:80] or "item").strip("._")


def _iso_now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def list_exportable(
    extensions: list[ExtensionInfo],
    store: AppStore,
) -> list[TransferItemPreview]:
    out: list[TransferItemPreview] = []
    for ext in extensions:
        hist = store.get_history(ext.uid)
        if not hist:
            continue
        pkg = 0
        for h in hist:
            if h.archive_path and Path(h.archive_path).is_file():
                pkg += 1
            elif h.snapshot_path and Path(h.snapshot_path).is_dir():
                pkg += 1
        if pkg == 0:
            continue
        out.append(
            TransferItemPreview(
                uid=ext.uid,
                name=ext.name,
                version=ext.version,
                path=ext.path,
                history_count=len(hist),
                package_count=pkg,
            )
        )
    out.sort(key=lambda x: x.name.lower())
    return out


def export_history_bundle(
    dest_zip: Path,
    uids: list[str],
    extensions: list[ExtensionInfo],
    store: AppStore,
) -> TransferResult:
    by_uid = {e.uid: e for e in extensions}
    work = temp_root() / f"export_hist_{uuid.uuid4().hex}"
    root = work / "bundle"
    root.mkdir(parents=True, exist_ok=True)

    items_out: list[dict[str, Any]] = []
    packages = 0
    records = 0

    try:
        for uid in uids:
            ext = by_uid.get(uid)
            if ext is None:
                continue
            hist = store.get_history(uid)
            if not hist:
                continue

            item_id = _safe_seg(f"{ext.name}_{ext.ext_id or uid}") + "_" + uuid.uuid4().hex[:8]
            item_dir = root / "items" / item_id
            files_dir = item_dir / "files"
            files_dir.mkdir(parents=True, exist_ok=True)

            packed_history: list[dict[str, Any]] = []
            for rec in hist:
                rel_file = ""
                src_archive = Path(rec.archive_path) if rec.archive_path else None
                src_snap = Path(rec.snapshot_path) if rec.snapshot_path else None

                if src_archive and src_archive.is_file():
                    suffix = src_archive.suffix.lower() or ".zip"
                    if suffix not in {".zip", ".7z", ".7zip"}:
                        suffix = ".zip"
                    rel_file = f"files/{rec.id}{suffix}"
                    shutil.copy2(src_archive, item_dir / rel_file)
                    packages += 1
                elif src_snap and src_snap.is_dir():
                    rel_file = f"files/{rec.id}.zip"
                    create_zip(src_snap, item_dir / rel_file)
                    packages += 1
                else:
                    # Keep metadata-only history (cannot re-export package)
                    pass

                packed = rec.to_dict()
                packed["bundle_file"] = rel_file
                # Do not leak absolute machine paths into the share package
                packed["archive_path"] = ""
                packed["snapshot_path"] = ""
                packed_history.append(packed)
                records += 1

            if not any(h.get("bundle_file") for h in packed_history):
                shutil.rmtree(item_dir, ignore_errors=True)
                continue

            meta = store.get_meta(uid)
            item_meta = {
                "item_id": item_id,
                "name": ext.name,
                "ext_id": ext.ext_id,
                "version": ext.version,
                "description": ext.description,
                "original_uid": uid,
                "original_path": ext.path,
                "browser": ext.browser.value,
                "profile": ext.profile,
                "install_source": ext.install_source,
                "category": meta.category,
                "history": packed_history,
            }
            (item_dir / "meta.json").write_text(
                json.dumps(item_meta, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            items_out.append(
                {
                    "item_id": item_id,
                    "name": ext.name,
                    "ext_id": ext.ext_id,
                    "package_count": sum(1 for h in packed_history if h.get("bundle_file")),
                }
            )

        if not items_out:
            return TransferResult(False, "没有可导出的历史压缩包（请先上传过更新包，或已有快照）")

        manifest = {
            "format": FORMAT_ID,
            "app": "本地插件管理器",
            "exported_at": _iso_now(),
            "item_count": len(items_out),
            "items": items_out,
        }
        (root / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        dest_zip = dest_zip.resolve()
        dest_zip.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest_zip.with_suffix(dest_zip.suffix + ".partial")
        if tmp.exists():
            tmp.unlink()
        with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for path in root.rglob("*"):
                if path.is_file():
                    zf.write(path, path.relative_to(root).as_posix())
        if dest_zip.exists():
            dest_zip.unlink()
        tmp.replace(dest_zip)

        return TransferResult(
            ok=True,
            message=(
                f"已导出 {len(items_out)} 个插件的历史包\n"
                f"含 {packages} 个版本压缩包，共 {records} 条历史记录\n"
                f"保存到：\n{dest_zip}"
            ),
            records=records,
            packages=packages,
        )
    except Exception as e:
        log.exception("export_history_bundle failed")
        return TransferResult(False, f"导出失败: {e}")
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _match_extension(
    item: dict[str, Any],
    extensions: list[ExtensionInfo],
) -> ExtensionInfo | None:
    ext_id = (item.get("ext_id") or "").strip()
    name = (item.get("name") or "").strip().casefold()
    orig_path = (item.get("original_path") or "").strip()
    orig_key = ""
    if orig_path:
        try:
            orig_key = str(Path(orig_path).resolve()).casefold()
        except OSError:
            orig_key = orig_path.replace("\\", "/").casefold()

    # 1) exact path
    if orig_key:
        for e in extensions:
            try:
                if str(Path(e.path).resolve()).casefold() == orig_key:
                    return e
            except OSError:
                if e.path.replace("\\", "/").casefold() == orig_key:
                    return e

    # 2) ext_id + prefer local
    if ext_id:
        cands = [e for e in extensions if e.ext_id == ext_id]
        if cands:
            cands.sort(key=lambda e: (0 if e.install_source == "local" else 1, e.name))
            return cands[0]

    # 3) exact name + prefer local
    if name:
        cands = [e for e in extensions if e.name.strip().casefold() == name]
        if cands:
            cands.sort(key=lambda e: (0 if e.install_source == "local" else 1, e.name))
            return cands[0]
    return None


def import_history_bundle(
    src_zip: Path,
    extensions: list[ExtensionInfo],
    store: AppStore,
) -> TransferResult:
    if not src_zip.is_file():
        return TransferResult(False, "文件不存在")

    work = temp_root() / f"import_hist_{uuid.uuid4().hex}"
    extract_dir = work / "extracted"
    extract_dir.mkdir(parents=True, exist_ok=True)

    matched = orphaned = records = packages = 0
    created_orphans: list[dict[str, Any]] = []

    try:
        # Use hardened extractor (zip-slip / bomb / symlink checks)
        try:
            extract_archive(src_zip, extract_dir)
        except ArchiveError as e:
            return TransferResult(False, f"历史包解压失败: {e}")

        manifest_path = extract_dir / "manifest.json"
        if not manifest_path.is_file():
            return TransferResult(False, "不是有效的历史包（缺少 manifest.json）")
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return TransferResult(False, "历史包 manifest.json 无效")
        if not isinstance(manifest, dict) or manifest.get("format") != FORMAT_ID:
            return TransferResult(
                False,
                f"不支持的历史包格式: {manifest.get('format')!r}",
            )

        items_root = extract_dir / "items"
        if not items_root.is_dir():
            return TransferResult(False, "历史包内容为空")

        for item_dir in sorted(p for p in items_root.iterdir() if p.is_dir()):
            meta_file = item_dir / "meta.json"
            if not meta_file.is_file():
                continue
            try:
                item = json.loads(meta_file.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
            if not isinstance(item, dict):
                continue
            history_raw = item.get("history") or []
            if not history_raw:
                continue

            target = _match_extension(item, extensions)
            if target is not None:
                uid = target.uid
                matched += 1
            else:
                item_id = item.get("item_id") or uuid.uuid4().hex[:10]
                uid = f"imported|{_safe_seg(item.get('name') or 'plugin')}|{item_id}"
                orphaned += 1
                created_orphans.append(
                    {
                        "uid": uid,
                        "name": item.get("name") or "导入的插件",
                        "ext_id": item.get("ext_id") or "",
                        "version": item.get("version") or "",
                        "description": item.get("description")
                        or "从历史包导入（本机未找到对应插件，仍可从历史导出版本包）",
                        "path": item.get("original_path") or "",
                        "browser": item.get("browser") or "unknown",
                        "profile": "导入",
                        "install_source": "imported",
                        "category": item.get("category") or "未分类",
                    }
                )

            dest_dir = archives_root() / "imported" / _safe_seg(uid)
            dest_dir.mkdir(parents=True, exist_ok=True)

            existing_ids = {h.id for h in store.get_history(uid)}
            meta = store.get_meta(uid)
            if item.get("category"):
                meta.category = str(item["category"])[:64]
                if meta.category not in store.categories:
                    store.categories.append(meta.category)

            for h in history_raw:
                if not isinstance(h, dict):
                    continue
                rec = VersionRecord.from_dict(h)
                # Avoid id collision
                if rec.id in existing_ids:
                    rec.id = f"{rec.id}_imp_{uuid.uuid4().hex[:6]}"
                existing_ids.add(rec.id)

                bundle_file = str(h.get("bundle_file") or "")
                rec.snapshot_path = ""
                rec.archive_path = ""
                if bundle_file:
                    # Only allow relative files/... under this item_dir
                    rel = bundle_file.replace("\\", "/").lstrip("/")
                    if (
                        not rel.startswith("files/")
                        or ".." in rel.split("/")
                        or re.match(r"^[A-Za-z]:", rel)
                    ):
                        continue
                    src_file = (item_dir / rel).resolve()
                    try:
                        src_file.relative_to(item_dir.resolve())
                    except ValueError:
                        continue
                    if src_file.is_file():
                        safe_name = Path(src_file.name).name
                        dest_file = dest_dir / safe_name
                        if dest_file.exists():
                            dest_file = dest_dir / f"{rec.id}{src_file.suffix}"
                        shutil.copy2(src_file, dest_file)
                        rec.archive_path = str(dest_file.resolve())
                        packages += 1

                note_prefix = "【导入】"
                if note_prefix not in (rec.note or ""):
                    rec.note = f"{note_prefix}{rec.note}".strip()
                if not rec.label:
                    rec.label = f"导入 {rec.version}"
                meta.history.append(rec)
                records += 1

            # Keep a single latest flag
            if meta.history:
                for x in meta.history:
                    x.is_latest = False
                meta.history[-1].is_latest = True

        if created_orphans:
            store.add_imported_plugins(created_orphans)

        store.save()
        if matched == 0 and orphaned == 0:
            return TransferResult(False, "历史包里没有可导入的内容")

        return TransferResult(
            ok=True,
            message=(
                f"导入完成：匹配到本机插件 {matched} 个，新建导入项 {orphaned} 个\n"
                f"写入历史 {records} 条，版本压缩包 {packages} 个\n"
                "可在「版本历史」中导出 ZIP / 7z 发给使用。"
            ),
            matched=matched,
            orphaned=orphaned,
            records=records,
            packages=packages,
        )
    except zipfile.BadZipFile:
        return TransferResult(False, "文件不是有效的 ZIP 历史包")
    except Exception as e:
        log.exception("import_history_bundle failed")
        return TransferResult(False, f"导入失败: {e}")
    finally:
        shutil.rmtree(work, ignore_errors=True)


def imported_as_extensions(store: AppStore) -> list[ExtensionInfo]:
    """Virtual rows for history packages that did not match a local plugin."""
    out: list[ExtensionInfo] = []
    for d in store.imported_plugins:
        uid = d.get("uid") or ""
        if not uid:
            continue
        # Skip if already present as a real scan result uid collision — caller merges
        try:
            browser = BrowserKind(d.get("browser") or "unknown")
        except ValueError:
            browser = BrowserKind.UNKNOWN
        out.append(
            ExtensionInfo(
                uid=uid,
                browser=browser,
                profile=d.get("profile") or "导入",
                ext_id=d.get("ext_id") or "",
                name=d.get("name") or "导入的插件",
                version=d.get("version") or "",
                description=d.get("description") or "",
                path=d.get("path") or "",
                install_source="imported",
                locations=d.get("profile") or "导入",
            )
        )
    return out
