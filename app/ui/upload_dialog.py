from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import QObject, QThread, pyqtSignal
from PyQt6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressDialog,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
)

from app.core.downloader import DownloadError, DownloadResult, download_archive
from app.ui.privacy import PRIVACY_SUMMARY


class _DownloadWorker(QObject):
    finished = pyqtSignal(object)
    failed = pyqtSignal(str)
    progress = pyqtSignal(int, int)

    def __init__(self, url: str) -> None:
        super().__init__()
        self.url = url

    def run(self) -> None:
        try:
            result = download_archive(
                self.url,
                progress=lambda r, t: self.progress.emit(r, t),
            )
            self.finished.emit(result)
        except DownloadError as e:
            self.failed.emit(str(e))
        except Exception as e:  # noqa: BLE001
            self.failed.emit(str(e))


class UploadSourceDialog(QDialog):
    """Choose a local archive or a download URL (Google Drive / direct link)."""

    def __init__(self, ext_name: str, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"上传更新 — {ext_name}")
        self.resize(640, 300)
        self.selected_path: Path | None = None
        self.source_note: str = ""
        self._download_dir_to_cleanup: Path | None = None

        layout = QVBoxLayout(self)
        layout.addWidget(
            QLabel(
                "选择本地压缩包，或填写下载链接（支持 Google Drive "
                "`https://drive.google.com/file/d/...` 以及其它直接下载链接）。"
            )
        )
        privacy = QLabel(
            f"隐私：{PRIVACY_SUMMARY}\n"
            "粘贴的链接只用于本次本机下载，不会被软件发送给其他人。\n"
            "安全：仅允许 https://；禁止本机/内网地址；解压有路径穿越与体积限制。"
        )
        privacy.setWordWrap(True)
        privacy.setStyleSheet(
            "color:#255; background:#eef6f2; border:1px solid #b7d0c4;"
            " border-radius:4px; padding:8px;"
        )
        layout.addWidget(privacy)

        self.radio_local = QRadioButton("本地文件")
        self.radio_url = QRadioButton("下载链接")
        self.radio_local.setChecked(True)
        layout.addWidget(self.radio_local)

        row = QHBoxLayout()
        self.path_edit = QLineEdit()
        self.path_edit.setPlaceholderText("选择 .7z / .zip 文件…")
        btn_browse = QPushButton("浏览…")
        btn_browse.clicked.connect(self._browse)
        row.addWidget(self.path_edit, 1)
        row.addWidget(btn_browse)
        layout.addLayout(row)

        layout.addWidget(self.radio_url)
        self.url_edit = QLineEdit()
        self.url_edit.setPlaceholderText(
            "https://drive.google.com/file/d/xxxxxxxx/view?usp=sharing"
        )
        layout.addWidget(self.url_edit)

        self.radio_local.toggled.connect(self._sync_mode)
        self._sync_mode()

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("继续")
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _sync_mode(self) -> None:
        local = self.radio_local.isChecked()
        self.path_edit.setEnabled(local)
        self.url_edit.setEnabled(not local)

    def _browse(self) -> None:
        self.radio_local.setChecked(True)
        path, _ = QFileDialog.getOpenFileName(
            self,
            "选择更新包",
            "",
            "压缩包 (*.7z *.7zip *.zip);;所有文件 (*.*)",
        )
        if path:
            self.path_edit.setText(path)

    def _on_accept(self) -> None:
        if self.radio_local.isChecked():
            text = self.path_edit.text().strip().strip('"')
            if not text:
                QMessageBox.warning(self, "提示", "请选择本地压缩包文件")
                return
            path = Path(text)
            if not path.is_file():
                QMessageBox.warning(self, "提示", "文件不存在")
                return
            self.selected_path = path
            self.source_note = f"本地文件 {path.name}"
            self.accept()
            return

        url = self.url_edit.text().strip()
        if not url:
            QMessageBox.warning(self, "提示", "请填写下载链接")
            return
        if self._download(url):
            self.accept()

    def _download(self, url: str) -> bool:
        progress = QProgressDialog("正在下载压缩包…", "取消", 0, 0, self)
        progress.setWindowTitle("下载中")
        progress.setMinimumDuration(0)
        progress.setCancelButton(None)  # avoid half-written cancel complexity
        progress.setModal(True)
        progress.show()

        thread = QThread(self)
        worker = _DownloadWorker(url)
        worker.moveToThread(thread)

        result_box: dict[str, DownloadResult | None] = {"r": None}
        error_box: dict[str, str] = {"e": ""}

        def on_progress(received: int, total: int) -> None:
            if total > 0:
                progress.setRange(0, total)
                progress.setValue(min(received, total))
                mb = received / (1024 * 1024)
                total_mb = total / (1024 * 1024)
                progress.setLabelText(f"正在下载… {mb:.1f} / {total_mb:.1f} MB")
            else:
                progress.setRange(0, 0)
                progress.setLabelText(f"正在下载… {received / (1024 * 1024):.1f} MB")

        def on_finished(result: object) -> None:
            result_box["r"] = result  # type: ignore[assignment]
            thread.quit()

        def on_failed(msg: str) -> None:
            error_box["e"] = msg
            thread.quit()

        worker.progress.connect(on_progress)
        worker.finished.connect(on_finished)
        worker.failed.connect(on_failed)
        thread.started.connect(worker.run)
        thread.start()

        # Wait without freezing event loop completely
        while thread.isRunning():
            from PyQt6.QtWidgets import QApplication

            QApplication.processEvents()
            thread.wait(50)

        progress.close()

        if error_box["e"]:
            QMessageBox.critical(self, "下载失败", error_box["e"])
            return False
        result = result_box["r"]
        if result is None:
            QMessageBox.critical(self, "下载失败", "未知错误")
            return False

        self.selected_path = result.path
        self._download_dir_to_cleanup = result.path.parent
        self.source_note = f"链接下载 {result.filename}"
        return True

    def cleanup_download(self) -> None:
        """Remove temporary download folder after update finishes."""
        if self._download_dir_to_cleanup and self._download_dir_to_cleanup.exists():
            import shutil

            shutil.rmtree(self._download_dir_to_cleanup, ignore_errors=True)
        self._download_dir_to_cleanup = None
