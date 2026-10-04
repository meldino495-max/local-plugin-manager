from __future__ import annotations

import logging
import shutil
from pathlib import Path

from PyQt6.QtCore import QSize, Qt
from PyQt6.QtGui import QAction, QColor, QFont, QIcon, QPixmap
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from app.core.archive import ArchiveError
from app.core.history_transfer import (
    export_history_bundle,
    import_history_bundle,
    imported_as_extensions,
    list_exportable,
)
from app.core.scanner import scan_all_extensions
from app.core.updater import ExtensionUpdater, cleanup_work_dir
from app.models.extension import ExtensionInfo
from app.models.store import AppStore
from app.ui.cache_dialog import CacheDirDialog
from app.ui.privacy import PRIVACY_SUMMARY, show_privacy_dialog
from app.ui.transfer_dialog import ExportHistoryDialog
from app.ui.upload_dialog import UploadSourceDialog
from app.ui.version_dialog import VersionDialog
from app.utils.paths import data_dir

log = logging.getLogger(__name__)

COL_PIN = 0
COL_ICON = 1
COL_NAME = 2
COL_SOURCE = 3
COL_VERSION = 4
COL_BROWSER = 5
COL_PROFILE = 6
COL_CATEGORY = 7
COL_PATH = 8
COL_UPLOAD = 9
COL_HISTORY = 10
COL_COUNT = 11


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.store = AppStore()
        self.updater = ExtensionUpdater(self.store)
        self.extensions: list[ExtensionInfo] = []
        self._pending_work: Path | None = None
        self._icon_cache: dict[str, QIcon] = {}

        self.setWindowTitle("本地插件管理器")
        self.resize(1360, 740)
        self._build_ui()
        self.refresh()

    def _build_ui(self) -> None:
        toolbar = QToolBar("main")
        toolbar.setMovable(False)
        self.addToolBar(toolbar)

        act_refresh = QAction("刷新扫描", self)
        act_refresh.triggered.connect(self.refresh)
        toolbar.addAction(act_refresh)

        act_cat = QAction("新建分类", self)
        act_cat.triggered.connect(self._add_category)
        toolbar.addAction(act_cat)

        act_cache = QAction("缓存目录", self)
        act_cache.setToolTip("自定义版本快照、压缩包、临时文件和日志的存放位置")
        act_cache.triggered.connect(self._configure_cache_dir)
        toolbar.addAction(act_cache)

        act_privacy = QAction("隐私说明", self)
        act_privacy.setToolTip(PRIVACY_SUMMARY)
        act_privacy.triggered.connect(lambda: show_privacy_dialog(self))
        toolbar.addAction(act_privacy)

        toolbar.addSeparator()
        act_export_hist = QAction("导出历史包", self)
        act_export_hist.setToolTip("把插件版本历史和压缩包打成 ZIP，发给别人导入")
        act_export_hist.triggered.connect(self._export_history)
        toolbar.addAction(act_export_hist)

        act_import_hist = QAction("导入历史包", self)
        act_import_hist.setToolTip("导入别人分享的历史包，之后可在版本历史中导出 ZIP/7z")
        act_import_hist.triggered.connect(self._import_history)
        toolbar.addAction(act_import_hist)

        toolbar.addSeparator()
        self.btn_hide_store = QPushButton()
        self.btn_hide_store.setCheckable(True)
        self.btn_hide_store.setChecked(self.store.hide_webstore)
        self.btn_hide_store.clicked.connect(self._toggle_hide_store)
        self._sync_hide_store_button()
        toolbar.addWidget(self.btn_hide_store)

        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)

        tip = QLabel(
            "默认只显示本地扩展；可用「商店扩展」按钮切换。"
            "「上传更新」支持本地文件或下载链接；「导出/导入历史包」可分享版本包；"
            "「缓存目录」可自定义数据存放位置。\n"
            f"隐私：{PRIVACY_SUMMARY}"
        )
        tip.setWordWrap(True)
        tip.setStyleSheet("color:#445; padding:4px 2px;")
        layout.addWidget(tip)

        filters = QHBoxLayout()
        filters.addWidget(QLabel("浏览器"))
        self.filter_browser = QComboBox()
        self.filter_browser.addItem("全部", "")
        for label, value in [
            ("Chrome", "chrome"),
            ("Edge", "edge"),
            ("Brave", "brave"),
            ("Firefox", "firefox"),
        ]:
            self.filter_browser.addItem(label, value)
        self.filter_browser.currentIndexChanged.connect(self._apply_filter)
        filters.addWidget(self.filter_browser)

        filters.addWidget(QLabel("分类"))
        self.filter_category = QComboBox()
        self.filter_category.currentIndexChanged.connect(self._apply_filter)
        filters.addWidget(self.filter_category)

        filters.addWidget(QLabel("来源"))
        self.filter_source = QComboBox()
        self.filter_source.addItem("全部来源", "")
        self.filter_source.addItem("仅本地", "local")
        self.filter_source.addItem("仅商店", "webstore")
        self.filter_source.addItem("仅导入", "imported")
        self.filter_source.currentIndexChanged.connect(self._apply_filter)
        filters.addWidget(self.filter_source)

        filters.addWidget(QLabel("搜索"))
        self.search = QLineEdit()
        self.search.setPlaceholderText("名称 / ID / 路径…")
        self.search.textChanged.connect(self._apply_filter)
        filters.addWidget(self.search, 1)

        self.chk_pinned_only = QComboBox()
        self.chk_pinned_only.addItem("全部", "all")
        self.chk_pinned_only.addItem("仅置顶", "pinned")
        self.chk_pinned_only.currentIndexChanged.connect(self._apply_filter)
        filters.addWidget(self.chk_pinned_only)

        layout.addLayout(filters)

        self.table = QTableWidget(0, COL_COUNT)
        self.table.setHorizontalHeaderLabels(
            [
                "置顶",
                "图标",
                "名称",
                "来源",
                "版本",
                "浏览器",
                "位置",
                "分类",
                "路径",
                "上传更新",
                "版本历史",
            ]
        )
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        self.table.setIconSize(QSize(28, 28))
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(COL_PIN, QHeaderView.ResizeMode.Fixed)
        self.table.setColumnWidth(COL_PIN, 44)
        header.setSectionResizeMode(COL_ICON, QHeaderView.ResizeMode.Fixed)
        self.table.setColumnWidth(COL_ICON, 40)
        header.setSectionResizeMode(COL_NAME, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(COL_PATH, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(COL_UPLOAD, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(COL_HISTORY, QHeaderView.ResizeMode.ResizeToContents)
        self.table.cellDoubleClicked.connect(self._on_cell_double_click)
        layout.addWidget(self.table, 1)

        self.status = QLabel("")
        layout.addWidget(self.status)

    def _sync_hide_store_button(self) -> None:
        if self.store.hide_webstore:
            self.btn_hide_store.setText("商店扩展：已隐藏")
            self.btn_hide_store.setToolTip("当前已隐藏谷歌/Edge 商店下载的扩展。点击后显示它们。")
            self.btn_hide_store.setStyleSheet(
                "QPushButton { padding:4px 10px; background:#eef6ee; border:1px solid #9cbc9c; }"
            )
        else:
            self.btn_hide_store.setText("商店扩展：显示中")
            self.btn_hide_store.setToolTip("当前正在显示商店扩展。点击后隐藏它们。")
            self.btn_hide_store.setStyleSheet(
                "QPushButton { padding:4px 10px; background:#fff6e8; border:1px solid #d2b48c; }"
            )

    def _toggle_hide_store(self) -> None:
        self.store.set_hide_webstore(self.btn_hide_store.isChecked())
        self._sync_hide_store_button()
        self._apply_filter()

    def _reload_category_filter(self) -> None:
        current = self.filter_category.currentData()
        self.filter_category.blockSignals(True)
        self.filter_category.clear()
        self.filter_category.addItem("全部", "")
        for c in self.store.categories:
            self.filter_category.addItem(c, c)
        idx = self.filter_category.findData(current)
        self.filter_category.setCurrentIndex(idx if idx >= 0 else 0)
        self.filter_category.blockSignals(False)

    def refresh(self) -> None:
        try:
            scanned = scan_all_extensions()
        except Exception as e:
            log.exception("scan failed")
            QMessageBox.critical(self, "扫描失败", str(e))
            scanned = []
        scanned_uids = {e.uid for e in scanned}
        # Also index by path for dropping redundant imported orphans
        scanned_paths = set()
        for e in scanned:
            if e.path:
                try:
                    scanned_paths.add(str(Path(e.path).resolve()).casefold())
                except OSError:
                    scanned_paths.add(e.path.replace("\\", "/").casefold())

        orphans: list[ExtensionInfo] = []
        for imp in imported_as_extensions(self.store):
            if imp.uid in scanned_uids:
                continue
            if imp.path:
                try:
                    key = str(Path(imp.path).resolve()).casefold()
                except OSError:
                    key = imp.path.replace("\\", "/").casefold()
                if key in scanned_paths:
                    continue
            orphans.append(imp)

        self.extensions = scanned + orphans
        self._icon_cache.clear()
        self._reload_category_filter()
        self._apply_filter()

    def _icon_for(self, ext: ExtensionInfo) -> QIcon:
        key = ext.icon_path or ""
        if key in self._icon_cache:
            return self._icon_cache[key]
        icon = QIcon()
        if key and Path(key).is_file():
            # Guard against malformed image crashes in Qt image decoders
            try:
                pix = QPixmap(key)
                if not pix.isNull():
                    icon = QIcon(
                        pix.scaled(
                            28,
                            28,
                            Qt.AspectRatioMode.KeepAspectRatio,
                            Qt.TransformationMode.SmoothTransformation,
                        )
                    )
            except Exception:
                log.debug("icon load failed: %s", key, exc_info=True)
                icon = QIcon()
        self._icon_cache[key] = icon
        return icon

    def _filtered(self) -> list[ExtensionInfo]:
        browser = self.filter_browser.currentData() or ""
        category = self.filter_category.currentData() or ""
        source = self.filter_source.currentData() or ""
        pinned_mode = self.chk_pinned_only.currentData() or "all"
        q = (self.search.text() or "").strip().lower()

        items = list(self.extensions)
        items.sort(
            key=lambda e: (
                0 if self.store.get_meta(e.uid).pinned else 1,
                e.browser.value,
                e.name.lower(),
            )
        )

        out: list[ExtensionInfo] = []
        for e in items:
            meta = self.store.get_meta(e.uid)
            if self.store.hide_webstore and e.is_webstore:
                continue
            if self.store.hide_component and e.is_component:
                continue
            # imported rows are always shown unless filtered by source
            if browser and e.browser.value != browser:
                continue
            if category and meta.category != category:
                continue
            if source and e.install_source != source:
                continue
            if pinned_mode == "pinned" and not meta.pinned:
                continue
            if q:
                blob = (
                    f"{e.name} {e.ext_id} {e.path} {e.version} "
                    f"{meta.category} {e.source_label}"
                ).lower()
                if q not in blob:
                    continue
            out.append(e)
        return out

    def _apply_filter(self) -> None:
        rows = self._filtered()
        self.table.setRowCount(len(rows))
        self._row_exts = rows

        pin_font = QFont()
        pin_font.setBold(True)

        for r, ext in enumerate(rows):
            meta = self.store.get_meta(ext.uid)
            self.table.setRowHeight(r, 36)

            pin_btn = QPushButton("★" if meta.pinned else "☆")
            pin_btn.setFixedSize(32, 28)
            pin_btn.setToolTip("取消置顶" if meta.pinned else "置顶")
            pin_btn.setStyleSheet(
                "QPushButton { padding:0; margin:0; }"
                + (" color:#c48a00; font-weight:700;" if meta.pinned else "")
            )
            pin_btn.clicked.connect(lambda _=False, u=ext.uid: self._toggle_pin(u))
            pin_wrap = QWidget()
            pin_layout = QHBoxLayout(pin_wrap)
            pin_layout.setContentsMargins(4, 0, 4, 0)
            pin_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
            pin_layout.addWidget(pin_btn)
            self.table.setCellWidget(r, COL_PIN, pin_wrap)

            icon_item = QTableWidgetItem()
            icon_item.setIcon(self._icon_for(ext))
            icon_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            icon_item.setFlags(icon_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.table.setItem(r, COL_ICON, icon_item)

            name_item = QTableWidgetItem(ext.name)
            if meta.pinned:
                name_item.setFont(pin_font)
                name_item.setForeground(QColor("#8a5a00"))
            name_item.setData(Qt.ItemDataRole.UserRole, ext.uid)
            name_item.setToolTip(ext.description or ext.name)
            self.table.setItem(r, COL_NAME, name_item)

            src_item = QTableWidgetItem(ext.source_label)
            if ext.is_webstore:
                src_item.setForeground(QColor("#8a5a00"))
            elif ext.is_component:
                src_item.setForeground(QColor("#666"))
            elif ext.install_source == "imported":
                src_item.setForeground(QColor("#355c9b"))
            else:
                src_item.setForeground(QColor("#2a6b2a"))
            self.table.setItem(r, COL_SOURCE, src_item)

            self.table.setItem(r, COL_VERSION, QTableWidgetItem(ext.version))
            self.table.setItem(r, COL_BROWSER, QTableWidgetItem(ext.browser.label))
            self.table.setItem(r, COL_PROFILE, QTableWidgetItem(ext.profile))

            cat = QComboBox()
            for c in self.store.categories:
                cat.addItem(c)
            idx = cat.findText(meta.category)
            cat.setCurrentIndex(idx if idx >= 0 else 0)
            cat.currentTextChanged.connect(
                lambda text, u=ext.uid: self._set_category(u, text)
            )
            self.table.setCellWidget(r, COL_CATEGORY, cat)

            path_item = QTableWidgetItem(ext.path)
            path_item.setToolTip(ext.path)
            self.table.setItem(r, COL_PATH, path_item)

            upload_btn = QPushButton("上传更新")
            upload_btn.setToolTip(
                "选择本地 7z/zip，或填写 Google Drive / 其它下载链接，解压后覆盖写入"
            )
            upload_btn.clicked.connect(lambda _=False, e=ext: self._upload_for(e))
            self.table.setCellWidget(r, COL_UPLOAD, upload_btn)

            hist_n = len(meta.history)
            hist_btn = QPushButton(f"历史({hist_n})")
            hist_btn.clicked.connect(lambda _=False, e=ext: self._open_history(e))
            self.table.setCellWidget(r, COL_HISTORY, hist_btn)

        local_n = sum(1 for e in self.extensions if e.install_source == "local")
        store_n = sum(1 for e in self.extensions if e.is_webstore)
        comp_n = sum(1 for e in self.extensions if e.is_component)
        self.status.setText(
            f"显示 {len(rows)} 个｜本地 {local_n}｜商店 {store_n}｜系统组件 {comp_n}｜扫描总计 {len(self.extensions)}"
        )

    def _toggle_pin(self, uid: str) -> None:
        meta = self.store.get_meta(uid)
        self.store.set_pinned(uid, not meta.pinned)
        self._apply_filter()

    def _set_category(self, uid: str, category: str) -> None:
        self.store.set_category(uid, category)

    def _add_category(self) -> None:
        text, ok = QInputDialog.getText(self, "新建分类", "分类名称：")
        if not ok:
            return
        text = text.strip()
        if not text:
            return
        self.store.add_category(text)
        self._reload_category_filter()
        self._apply_filter()

    def _configure_cache_dir(self) -> None:
        dlg = CacheDirDialog(self)
        if dlg.exec() and dlg.changed:
            # Reload store from the new location
            self.store = AppStore()
            self.updater = ExtensionUpdater(self.store)
            self.btn_hide_store.setChecked(self.store.hide_webstore)
            self._sync_hide_store_button()
            self.refresh()
            self.status.setText(f"缓存目录：{data_dir()}")

    def _export_history(self) -> None:
        items = list_exportable(self.extensions, self.store)
        if not items:
            QMessageBox.information(
                self,
                "提示",
                "当前没有可导出的历史压缩包。\n请先对插件使用「上传 7z」产生版本历史。",
            )
            return
        dlg = ExportHistoryDialog(items, self)
        if not dlg.exec():
            return
        from datetime import datetime

        default_name = f"插件历史包_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip"
        path, _ = QFileDialog.getSaveFileName(
            self,
            "保存历史包",
            default_name,
            "历史包 ZIP (*.zip);;所有文件 (*.*)",
        )
        if not path:
            return
        dest = Path(path)
        if dest.suffix.lower() != ".zip":
            dest = dest.with_suffix(".zip")
        result = export_history_bundle(dest, dlg.selected_uids, self.extensions, self.store)
        if result.ok:
            QMessageBox.information(self, "导出完成", result.message)
        else:
            QMessageBox.critical(self, "导出失败", result.message)

    def _import_history(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "选择历史包",
            "",
            "历史包 ZIP (*.zip);;所有文件 (*.*)",
        )
        if not path:
            return
        result = import_history_bundle(Path(path), self.extensions, self.store)
        if result.ok:
            QMessageBox.information(self, "导入完成", result.message)
            self.refresh()
        else:
            QMessageBox.critical(self, "导入失败", result.message)

    def _on_cell_double_click(self, row: int, col: int) -> None:
        if col != COL_PATH:
            return
        if row < 0 or row >= len(self._row_exts):
            return
        ext = self._row_exts[row]
        path = ext.path
        if Path(path).exists():
            from app.utils.shell import safe_open_path
            from app.utils.paths import project_root, data_dir

            # Allow opening the installed plugin folder itself
            roots = [project_root(), data_dir()]
            try:
                roots.append(Path(path).resolve())
            except OSError:
                pass
            ok, err = safe_open_path(path, allow_roots=roots)
            if not ok:
                QMessageBox.warning(self, "无法打开", err)

    def _upload_for(self, ext: ExtensionInfo) -> None:
        if ext.install_source == "imported" and (not ext.path or not Path(ext.path).is_dir()):
            QMessageBox.information(
                self,
                "提示",
                "这是导入的历史项，本机没有对应插件目录。\n"
                "请打开「版本历史」导出 ZIP/7z；若要更新本机插件，请先安装/加载该扩展。",
            )
            return
        if Path(ext.path).is_file() and ext.path.lower().endswith(".xpi"):
            QMessageBox.warning(
                self,
                "不支持",
                "该扩展是打包的 .xpi 文件。请先解压为文件夹后再用本工具管理。",
            )
            return

        src_dlg = UploadSourceDialog(ext.name, self)
        if not src_dlg.exec() or src_dlg.selected_path is None:
            src_dlg.cleanup_download()
            return

        archive = src_dlg.selected_path
        cleanup_work_dir(self._pending_work)
        self._pending_work = None

        try:
            work, preview = self.updater.prepare_upload(ext, archive)
            self._pending_work = work
        except ArchiveError as e:
            QMessageBox.critical(self, "解压/校验失败", str(e))
            src_dlg.cleanup_download()
            return
        except Exception as e:
            log.exception("prepare_upload failed")
            QMessageBox.critical(self, "失败", str(e))
            src_dlg.cleanup_download()
            return

        sample = "\n".join(f"  · {f}" for f in preview.sample_files[:15])
        more = ""
        if preview.file_count > 15:
            more = f"\n  … 等共 {preview.file_count} 个文件"
        msg = (
            f"目标扩展：{ext.name}\n"
            f"来源：{src_dlg.source_note}\n"
            f"当前版本：{ext.version or '未知'}\n"
            f"压缩包版本：{preview.package_version}\n"
            f"目标路径：\n{preview.target_path}\n\n"
            f"将覆盖写入 {preview.file_count} 个文件（不会删除压缩包中没有的旧文件）。\n"
            f"执行前会自动备份当前目录到版本历史。\n\n"
            f"文件预览：\n{sample}{more}\n\n"
            "确认继续？"
        )
        reply = QMessageBox.question(self, "确认更新", msg)
        if reply != QMessageBox.StandardButton.Yes:
            cleanup_work_dir(self._pending_work)
            self._pending_work = None
            src_dlg.cleanup_download()
            return

        try:
            result = self.updater.apply_upload(
                ext, archive, work, note=src_dlg.source_note or f"上传 {archive.name}"
            )
        finally:
            cleanup_work_dir(self._pending_work)
            self._pending_work = None
            src_dlg.cleanup_download()

        if result.ok:
            QMessageBox.information(self, "更新成功", result.message)
            self.refresh()
        else:
            QMessageBox.critical(self, "更新失败", result.message)

    def _open_history(self, ext: ExtensionInfo) -> None:
        dlg = VersionDialog(ext, self.store, self.updater, self)
        if dlg.exec():
            self.refresh()

    def closeEvent(self, event) -> None:  # noqa: N802
        cleanup_work_dir(self._pending_work)
        try:
            from app.utils.paths import temp_root

            for p in temp_root().glob("upload_*"):
                shutil.rmtree(p, ignore_errors=True)
            for p in temp_root().glob("restore_*"):
                shutil.rmtree(p, ignore_errors=True)
        except Exception:
            pass
        super().closeEvent(event)
