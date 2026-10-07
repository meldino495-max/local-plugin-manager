from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from app.core.updater import ExtensionUpdater
from app.models.extension import ExtensionInfo, VersionRecord
from app.models.store import AppStore


def _safe_filename(text: str) -> str:
    keep = []
    for ch in text:
        if ch.isalnum() or ch in ("-", "_", "."):
            keep.append(ch)
        else:
            keep.append("_")
    return "".join(keep).strip("._") or "extension"


class VersionDialog(QDialog):
    def __init__(
        self,
        ext: ExtensionInfo,
        store: AppStore,
        updater: ExtensionUpdater,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.ext = ext
        self.store = store
        self.updater = updater
        self.history_changed = False
        self.setWindowTitle(f"版本历史 — {ext.name}")
        self.resize(900, 480)
        self._rows: list[VersionRecord] = []
        self._build()
        self.reload()

    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.addWidget(
            QLabel(
                f"当前安装路径：{self.ext.path}\n"
                f"当前版本号：{self.ext.version or '未知'}\n"
                "可恢复历史版本、升到最新保存包、导出版本，或删除选中/全部历史记录。"
                "（按住 Ctrl / Shift 可多选）"
            )
        )

        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(
            ["版本号", "标签", "时间", "类型", "备注", "最新"]
        )
        self.table.setSelectionBehavior(self.table.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(self.table.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(self.table.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.table)

        row = QHBoxLayout()
        self.btn_restore = QPushButton("恢复选中版本")
        self.btn_latest = QPushButton("升级到最新保存版")
        self.btn_export_zip = QPushButton("导出 ZIP")
        self.btn_export_7z = QPushButton("导出 7z")
        self.btn_export_current = QPushButton("导出当前安装")
        self.btn_open = QPushButton("打开版本文件位置")
        self.btn_delete = QPushButton("删除选中")
        self.btn_clear = QPushButton("清空本插件历史")
        self.btn_delete.setToolTip("删除选中的历史记录，并清理对应快照/压缩包文件")
        self.btn_clear.setToolTip("删除该插件的全部历史记录与关联文件")

        self.btn_restore.clicked.connect(self._restore_selected)
        self.btn_latest.clicked.connect(self._restore_latest)
        self.btn_export_zip.clicked.connect(lambda: self._export_selected("zip"))
        self.btn_export_7z.clicked.connect(lambda: self._export_selected("7z"))
        self.btn_export_current.clicked.connect(self._export_current)
        self.btn_open.clicked.connect(self._open_files)
        self.btn_delete.clicked.connect(self._delete_selected)
        self.btn_clear.clicked.connect(self._clear_all)

        row.addWidget(self.btn_restore)
        row.addWidget(self.btn_latest)
        row.addWidget(self.btn_export_zip)
        row.addWidget(self.btn_export_7z)
        row.addWidget(self.btn_export_current)
        row.addWidget(self.btn_open)
        row.addWidget(self.btn_delete)
        row.addWidget(self.btn_clear)
        row.addStretch(1)
        layout.addLayout(row)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        buttons.button(QDialogButtonBox.StandardButton.Close).clicked.connect(self.reject)
        layout.addWidget(buttons)

    def reload(self) -> None:
        hist = list(reversed(self.store.get_history(self.ext.uid)))
        self._rows = hist
        self.table.setRowCount(len(hist))
        for i, rec in enumerate(hist):
            vals = [
                rec.version,
                rec.label,
                rec.created_at,
                rec.source,
                rec.note,
                "是" if rec.is_latest else "",
            ]
            for j, v in enumerate(vals):
                item = QTableWidgetItem(v)
                if j == 0:
                    item.setData(Qt.ItemDataRole.UserRole, rec.id)
                self.table.setItem(i, j, item)
        self.table.resizeColumnsToContents()
        has = bool(hist)
        self.btn_delete.setEnabled(has)
        self.btn_clear.setEnabled(has)

    def _selected_records(self) -> list[VersionRecord]:
        rows = self.table.selectionModel().selectedRows()
        out: list[VersionRecord] = []
        seen: set[str] = set()
        for model_idx in rows:
            idx = model_idx.row()
            if idx < 0 or idx >= len(self._rows):
                continue
            rec = self._rows[idx]
            if rec.id in seen:
                continue
            seen.add(rec.id)
            out.append(rec)
        return out

    def _selected_record(self) -> VersionRecord | None:
        selected = self._selected_records()
        return selected[0] if selected else None

    def _restore_selected(self) -> None:
        selected = self._selected_records()
        if not selected:
            QMessageBox.information(self, "提示", "请先选择一个版本")
            return
        if len(selected) > 1:
            QMessageBox.information(self, "提示", "恢复只能选择一个版本")
            return
        rec = selected[0]
        reply = QMessageBox.question(
            self,
            "确认恢复",
            f"确定恢复到版本 {rec.version}？\n恢复前会自动备份当前文件。",
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        result = self.updater.restore_record(self.ext, rec)
        if result.ok:
            self.history_changed = True
            QMessageBox.information(self, "完成", result.message)
            self.accept()
        else:
            QMessageBox.critical(self, "失败", result.message)

    def _restore_latest(self) -> None:
        reply = QMessageBox.question(
            self,
            "确认",
            "将恢复到已保存的最新上传包（或最近可用快照）。\n恢复前会自动备份当前文件。",
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        result = self.updater.restore_latest(self.ext)
        if result.ok:
            self.history_changed = True
            QMessageBox.information(self, "完成", result.message)
            self.accept()
        else:
            QMessageBox.critical(self, "失败", result.message)

    def _delete_selected(self) -> None:
        selected = self._selected_records()
        if not selected:
            QMessageBox.information(self, "提示", "请先选择要删除的历史记录（可多选）")
            return
        labels = "\n".join(
            f"· {r.version or '?'}  ({r.label or r.source})  {r.created_at}"
            for r in selected[:12]
        )
        more = "" if len(selected) <= 12 else f"\n… 另有 {len(selected) - 12} 条"
        reply = QMessageBox.question(
            self,
            "确认删除",
            f"确定删除选中的 {len(selected)} 条历史记录吗？\n"
            "对应的快照/压缩包文件也会一并删除，且不可恢复。\n\n"
            f"{labels}{more}",
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        n = self.store.delete_versions(
            self.ext.uid, [r.id for r in selected], delete_files=True
        )
        self.history_changed = True
        QMessageBox.information(self, "已删除", f"已删除 {n} 条历史记录。")
        self.reload()

    def _clear_all(self) -> None:
        hist = self.store.get_history(self.ext.uid)
        if not hist:
            QMessageBox.information(self, "提示", "当前没有历史记录")
            return
        reply = QMessageBox.warning(
            self,
            "清空本插件历史",
            f"确定清空「{self.ext.name}」的全部 {len(hist)} 条历史记录吗？\n"
            "快照与压缩包文件也会删除，且不可恢复。\n\n"
            "这不会卸载浏览器中的扩展本身。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        n = self.store.clear_history(self.ext.uid, delete_files=True)
        self.history_changed = True
        QMessageBox.information(self, "已清空", f"已删除 {n} 条历史记录。")
        self.reload()

    def _open_files(self) -> None:
        selected = self._selected_records()
        if not selected:
            QMessageBox.information(self, "提示", "请先选择一个版本")
            return
        if len(selected) > 1:
            QMessageBox.information(self, "提示", "打开位置时请只选择一个版本")
            return
        rec = selected[0]
        path = ""
        if rec.snapshot_path and Path(rec.snapshot_path).exists():
            path = rec.snapshot_path
        elif rec.archive_path and Path(rec.archive_path).exists():
            path = str(Path(rec.archive_path).parent)
        if not path:
            QMessageBox.warning(self, "提示", "该版本没有本地文件")
            return
        from app.utils.shell import safe_open_path

        ok, err = safe_open_path(path)
        if not ok:
            QMessageBox.warning(self, "无法打开", err)

    def _default_export_name(self, version: str, fmt: str) -> str:
        name = _safe_filename(self.ext.name)
        ver = _safe_filename(version or "unknown")
        ext = "7z" if fmt in {"7z", "7zip"} else "zip"
        return f"{name}_{ver}.{ext}"

    def _ask_export_path(self, default_name: str, fmt: str) -> Path | None:
        if fmt in {"7z", "7zip"}:
            filt = "7z 压缩包 (*.7z);;所有文件 (*.*)"
        else:
            filt = "ZIP 压缩包 (*.zip);;所有文件 (*.*)"
        path, _ = QFileDialog.getSaveFileName(self, "导出版本", default_name, filt)
        if not path:
            return None
        dest = Path(path)
        want = ".7z" if fmt in {"7z", "7zip"} else ".zip"
        if dest.suffix.lower() not in {want, ".7zip"}:
            dest = dest.with_suffix(want)
        return dest

    def _export_selected(self, fmt: str) -> None:
        selected = self._selected_records()
        if not selected:
            QMessageBox.information(self, "提示", "请先选择一个版本")
            return
        if len(selected) > 1:
            QMessageBox.information(self, "提示", "导出时请只选择一个版本")
            return
        rec = selected[0]
        dest = self._ask_export_path(self._default_export_name(rec.version, fmt), fmt)
        if dest is None:
            return
        result = self.updater.export_record(self.ext, rec, dest, fmt)
        if result.ok:
            QMessageBox.information(self, "导出完成", result.message)
        else:
            QMessageBox.critical(self, "导出失败", result.message)

    def _export_current(self) -> None:
        items = ["ZIP (.zip)", "7z (.7z)"]
        from PyQt6.QtWidgets import QInputDialog

        choice, ok = QInputDialog.getItem(
            self, "导出当前安装", "选择格式：", items, 0, False
        )
        if not ok:
            return
        fmt = "7z" if choice.startswith("7z") else "zip"
        dest = self._ask_export_path(
            self._default_export_name(self.ext.version or "current", fmt), fmt
        )
        if dest is None:
            return
        result = self.updater.export_current(self.ext, dest, fmt)
        if result.ok:
            QMessageBox.information(self, "导出完成", result.message)
        else:
            QMessageBox.critical(self, "导出失败", result.message)
