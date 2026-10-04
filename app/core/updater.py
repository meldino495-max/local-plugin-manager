from __future__ import annotations

import logging
import shutil
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from app.core.archive import (
    ArchiveError,
    _is_within,
    copy_tree_merge,
    create_archive,
    extract_archive,
    snapshot_directory,
)
from app.core.manifest import find_extension_root, parse_manifest
from app.models.extension import ExtensionInfo, VersionRecord
from app.models.store import AppStore
from app.utils.paths import archives_root, temp_root, versions_root

log = logging.getLogger(__name__)


@dataclass
class UpdatePreview:
    package_version: str
    package_name: str
    source_root: str
    target_path: str
    file_count: int
    sample_files: list[str] = field(default_factory=list)


@dataclass
class UpdateResult:
    ok: bool
    message: str
    new_version: str = ""
    record_id: str = ""
    written_count: int = 0


def _now_stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _iso_now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _safe_segment(text: str) -> str:
    keep = []
    for ch in text:
        if ch.isalnum() or ch in ("-", "_", "."):
            keep.append(ch)
        else:
            keep.append("_")
    return "".join(keep)[:80] or "ext"


class ExtensionUpdater:
    def __init__(self, store: AppStore) -> None:
        self.store = store

    def prepare_upload(self, ext: ExtensionInfo, archive_path: Path) -> tuple[Path, UpdatePreview]:
        """Extract archive to a temp work dir and return preview. Caller must cleanup work_dir parent."""
        if not archive_path.is_file():
            raise ArchiveError("压缩包不存在")
        target = Path(ext.path)
        if not target.is_dir():
            raise ArchiveError(f"插件目录不存在: {target}")

        work = temp_root() / f"upload_{uuid.uuid4().hex}"
        extract_dir = work / "extracted"
        extract_archive(archive_path, extract_dir)
        root = find_extension_root(extract_dir)
        if root is None:
            shutil.rmtree(work, ignore_errors=True)
            raise ArchiveError("压缩包中未找到 manifest.json，无法确认这是浏览器扩展")

        info = parse_manifest(root) or {}
        files = [
            str(p.relative_to(root)).replace("\\", "/")
            for p in root.rglob("*")
            if p.is_file() and "__MACOSX" not in p.parts and not p.name.startswith("._")
        ]
        preview = UpdatePreview(
            package_version=str(info.get("version") or "unknown"),
            package_name=str(info.get("name") or ext.name),
            source_root=str(root),
            target_path=str(target.resolve()),
            file_count=len(files),
            sample_files=files[:30],
        )
        # Stash paths on work dir marker
        (work / "source_root.txt").write_text(str(root), encoding="utf-8")
        return work, preview

    def apply_upload(
        self,
        ext: ExtensionInfo,
        archive_path: Path,
        work_dir: Path,
        note: str = "",
    ) -> UpdateResult:
        target = Path(ext.path).resolve()
        if not target.is_dir():
            return UpdateResult(False, f"目标目录不存在: {target}")

        source_txt = work_dir / "source_root.txt"
        if not source_txt.is_file():
            return UpdateResult(False, "内部错误：缺少解压信息，请重新选择压缩包")
        source_root = Path(source_txt.read_text(encoding="utf-8").strip())
        if not source_root.is_dir():
            return UpdateResult(False, "解压目录已丢失，请重新选择压缩包")
        # Ensure extracted source stays inside the temp work directory
        try:
            if not _is_within(work_dir.resolve(), source_root.resolve()):
                return UpdateResult(False, "解压目录校验失败，已中止更新")
        except OSError:
            return UpdateResult(False, "解压目录校验失败，已中止更新")

        info = parse_manifest(source_root) or {}
        new_version = str(info.get("version") or "unknown")
        stamp = _now_stamp()
        rid = uuid.uuid4().hex[:12]
        seg = _safe_segment(ext.uid)

        # 1) Snapshot current install for rollback
        snap_dir = versions_root() / seg / f"{_safe_segment(ext.version or 'prev')}_{stamp}_{rid}"
        try:
            snapshot_directory(target, snap_dir)
        except Exception as e:
            return UpdateResult(False, f"备份当前版本失败，已中止更新: {e}")

        snap_record = VersionRecord(
            id=f"snap_{rid}",
            version=ext.version or "unknown",
            label=f"更新前备份 {ext.version or '?'}",
            created_at=_iso_now(),
            source="snapshot",
            snapshot_path=str(snap_dir),
            note="自动备份（上传更新前）",
            is_latest=False,
        )
        self.store.add_version(ext.uid, snap_record, mark_latest=False)

        # 2) Keep original archive
        arch_dir = archives_root() / seg
        arch_dir.mkdir(parents=True, exist_ok=True)
        arch_dest = arch_dir / f"{_safe_segment(new_version)}_{stamp}_{rid}{archive_path.suffix.lower() or '.7z'}"
        try:
            shutil.copy2(archive_path, arch_dest)
        except Exception as e:
            log.warning("保存压缩包副本失败: %s", e)
            arch_dest = Path("")

        # 3) Merge copy into target
        try:
            written, _skipped = copy_tree_merge(source_root, target)
        except Exception as e:
            return UpdateResult(
                False,
                f"写入失败（当前目录已有自动备份，可从版本历史回滚）: {e}",
            )

        # 4) Record uploaded version as latest
        upload_record = VersionRecord(
            id=f"up_{rid}",
            version=new_version,
            label=f"上传 {new_version}",
            created_at=_iso_now(),
            source="upload",
            archive_path=str(arch_dest) if arch_dest else "",
            snapshot_path="",
            note=note or f"从 {archive_path.name} 更新",
            is_latest=True,
        )
        self.store.add_version(ext.uid, upload_record, mark_latest=True)

        return UpdateResult(
            ok=True,
            message=f"已更新到 {new_version}，写入 {len(written)} 个文件。请在浏览器中重新加载该扩展。",
            new_version=new_version,
            record_id=upload_record.id,
            written_count=len(written),
        )

    def restore_record(self, ext: ExtensionInfo, record: VersionRecord) -> UpdateResult:
        target = Path(ext.path).resolve()
        if not target.is_dir():
            return UpdateResult(False, f"目标目录不存在: {target}")

        # Prefer snapshot directory; else extract archive
        source_dir: Path | None = None
        work: Path | None = None
        try:
            if record.snapshot_path and Path(record.snapshot_path).is_dir():
                source_dir = Path(record.snapshot_path)
            elif record.archive_path and Path(record.archive_path).is_file():
                work = temp_root() / f"restore_{uuid.uuid4().hex}"
                extract_dir = work / "extracted"
                extract_archive(Path(record.archive_path), extract_dir)
                root = find_extension_root(extract_dir)
                if root is None:
                    return UpdateResult(False, "历史压缩包中找不到 manifest.json")
                source_dir = root
            else:
                return UpdateResult(False, "该版本没有可用的快照或压缩包文件")

            # Snapshot current before restore
            stamp = _now_stamp()
            rid = uuid.uuid4().hex[:12]
            seg = _safe_segment(ext.uid)
            snap_dir = versions_root() / seg / f"before_restore_{stamp}_{rid}"
            snapshot_directory(target, snap_dir)
            self.store.add_version(
                ext.uid,
                VersionRecord(
                    id=f"snap_{rid}",
                    version=ext.version or "unknown",
                    label=f"回滚前备份 {ext.version or '?'}",
                    created_at=_iso_now(),
                    source="snapshot",
                    snapshot_path=str(snap_dir),
                    note=f"恢复到 {record.version} 之前自动备份",
                    is_latest=False,
                ),
                mark_latest=False,
            )

            written, _ = copy_tree_merge(source_dir, target)
            self.store.mark_latest(ext.uid, record.id)
            # Also append a rollback marker pointing at same assets
            self.store.add_version(
                ext.uid,
                VersionRecord(
                    id=f"rb_{rid}",
                    version=record.version,
                    label=f"已恢复 {record.version}",
                    created_at=_iso_now(),
                    source="rollback",
                    archive_path=record.archive_path,
                    snapshot_path=record.snapshot_path,
                    note=f"从历史版本 {record.id} 恢复",
                    is_latest=True,
                ),
                mark_latest=True,
            )
            return UpdateResult(
                ok=True,
                message=f"已恢复到版本 {record.version}，写入 {len(written)} 个文件。请在浏览器中重新加载扩展。",
                new_version=record.version,
                record_id=record.id,
                written_count=len(written),
            )
        except Exception as e:
            return UpdateResult(False, f"恢复失败: {e}")
        finally:
            if work is not None:
                shutil.rmtree(work, ignore_errors=True)

    def restore_latest(self, ext: ExtensionInfo) -> UpdateResult:
        latest = self.store.latest_record(ext.uid)
        if latest is None:
            return UpdateResult(False, "没有已保存的版本历史")
        # Prefer newest upload with archive
        hist = self.store.get_history(ext.uid)
        candidate = None
        for h in reversed(hist):
            if h.source == "upload" and h.archive_path and Path(h.archive_path).is_file():
                candidate = h
                break
        if candidate is None:
            for h in reversed(hist):
                if h.snapshot_path and Path(h.snapshot_path).is_dir():
                    candidate = h
                    break
                if h.archive_path and Path(h.archive_path).is_file():
                    candidate = h
                    break
        if candidate is None:
            return UpdateResult(False, "找不到可恢复的最新版本文件")
        return self.restore_record(ext, candidate)

    def export_record(
        self,
        ext: ExtensionInfo,
        record: VersionRecord,
        dest_archive: Path,
        fmt: str,
    ) -> UpdateResult:
        """Export one remembered version to zip or 7z."""
        fmt = fmt.lower().lstrip(".")
        if fmt not in {"zip", "7z", "7zip"}:
            return UpdateResult(False, "仅支持导出为 zip 或 7z")

        # Fast path: already have archive in the requested format
        if record.archive_path and Path(record.archive_path).is_file():
            src_arch = Path(record.archive_path)
            src_suf = src_arch.suffix.lower()
            want_7z = fmt in {"7z", "7zip"}
            same = (want_7z and src_suf in {".7z", ".7zip"}) or (
                fmt == "zip" and src_suf == ".zip"
            )
            if same:
                try:
                    dest_archive.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src_arch, dest_archive)
                    return UpdateResult(
                        ok=True,
                        message=f"已导出到:\n{dest_archive}",
                        new_version=record.version,
                        record_id=record.id,
                    )
                except Exception as e:
                    return UpdateResult(False, f"复制压缩包失败: {e}")

        work: Path | None = None
        try:
            source_dir: Path | None = None
            if record.snapshot_path and Path(record.snapshot_path).is_dir():
                source_dir = Path(record.snapshot_path)
            elif record.archive_path and Path(record.archive_path).is_file():
                work = temp_root() / f"export_{uuid.uuid4().hex}"
                extract_dir = work / "extracted"
                extract_archive(Path(record.archive_path), extract_dir)
                root = find_extension_root(extract_dir)
                if root is None:
                    return UpdateResult(False, "历史压缩包中找不到 manifest.json，无法导出")
                source_dir = root
            else:
                return UpdateResult(False, "该版本没有可导出的快照或压缩包")

            create_archive(source_dir, dest_archive, fmt)
            return UpdateResult(
                ok=True,
                message=f"已导出版本 {record.version} 到:\n{dest_archive}",
                new_version=record.version,
                record_id=record.id,
            )
        except ArchiveError as e:
            return UpdateResult(False, str(e))
        except Exception as e:
            return UpdateResult(False, f"导出失败: {e}")
        finally:
            if work is not None:
                shutil.rmtree(work, ignore_errors=True)

    def export_current(self, ext: ExtensionInfo, dest_archive: Path, fmt: str) -> UpdateResult:
        """Export the currently installed extension directory."""
        target = Path(ext.path)
        if not target.is_dir():
            return UpdateResult(False, f"当前插件目录不存在: {target}")
        try:
            create_archive(target, dest_archive, fmt)
            return UpdateResult(
                ok=True,
                message=f"已导出当前版本 {ext.version or '?'} 到:\n{dest_archive}",
                new_version=ext.version or "",
            )
        except ArchiveError as e:
            return UpdateResult(False, str(e))
        except Exception as e:
            return UpdateResult(False, f"导出失败: {e}")


def cleanup_work_dir(work_dir: Path | None) -> None:
    if work_dir and work_dir.exists():
        shutil.rmtree(work_dir, ignore_errors=True)
