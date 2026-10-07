from __future__ import annotations

from pathlib import Path

from PyQt6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

from app.ui.privacy import PRIVACY_SUMMARY
from app.utils.paths import (
    data_dir,
    default_data_dir,
    is_default_data_dir,
    project_root,
    set_configured_data_dir,
)


class CacheDirDialog(QDialog):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("缓存目录设置")
        self.resize(640, 220)
        self.changed = False

        layout = QVBoxLayout(self)
        layout.addWidget(
            QLabel(
                "缓存目录用于存放：版本快照、上传/下载的压缩包、临时解压文件、日志和设置数据。\n"
                "可改到其它磁盘以节省软件所在盘空间。"
            )
        )
        privacy = QLabel(PRIVACY_SUMMARY + " 缓存数据仅保存在你指定的本机目录中。")
        privacy.setWordWrap(True)
        privacy.setStyleSheet(
            "color:#255; background:#eef6f2; border:1px solid #b7d0c4;"
            " border-radius:4px; padding:8px;"
        )
        layout.addWidget(privacy)

        layout.addWidget(QLabel("当前缓存目录："))
        self.edit = QLineEdit(str(data_dir()))
        layout.addWidget(self.edit)

        row = QHBoxLayout()
        btn_browse = QPushButton("浏览…")
        btn_default = QPushButton("恢复默认")
        btn_open = QPushButton("打开目录")
        btn_browse.clicked.connect(self._browse)
        btn_default.clicked.connect(self._set_default)
        btn_open.clicked.connect(self._open)
        row.addWidget(btn_browse)
        row.addWidget(btn_default)
        row.addWidget(btn_open)
        row.addStretch(1)
        layout.addLayout(row)

        layout.addWidget(
            QLabel(f"默认位置：{default_data_dir()}")
        )

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("保存")
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _browse(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "选择缓存目录", self.edit.text())
        if path:
            self.edit.setText(path)

    def _set_default(self) -> None:
        self.edit.setText(str(default_data_dir()))

    def _open(self) -> None:
        from app.utils.shell import safe_open_path

        p = Path(self.edit.text().strip() or str(data_dir()))
        try:
            p = p.expanduser().resolve()
        except OSError:
            QMessageBox.warning(self, "无法打开", "无效路径")
            return
        p.mkdir(parents=True, exist_ok=True)
        # Do not pass `p` as an allow-root (that would open arbitrary typed paths).
        ok, err = safe_open_path(
            p, allow_roots=[data_dir(), default_data_dir(), project_root()]
        )
        if not ok:
            QMessageBox.warning(self, "无法打开", err)

    def _save(self) -> None:
        text = self.edit.text().strip().strip('"')
        if not text:
            QMessageBox.warning(self, "提示", "请填写缓存目录")
            return
        new_dir = Path(text)
        old = data_dir().resolve()
        try:
            target = new_dir.expanduser().resolve()
        except OSError:
            target = new_dir.expanduser()

        if target == old:
            self.accept()
            return

        migrate = False
        if old.exists() and any(old.iterdir()):
            reply = QMessageBox.question(
                self,
                "迁移数据？",
                "是否把现有缓存（历史、压缩包、设置）复制到新目录？\n\n"
                "选「是」：复制并切换（推荐）\n"
                "选「否」：只切换目录，不复制旧数据",
                QMessageBox.StandardButton.Yes
                | QMessageBox.StandardButton.No
                | QMessageBox.StandardButton.Cancel,
            )
            if reply == QMessageBox.StandardButton.Cancel:
                return
            migrate = reply == QMessageBox.StandardButton.Yes

        ok, msg = set_configured_data_dir(target, migrate=migrate)
        if not ok:
            QMessageBox.critical(self, "失败", msg)
            return

        self.changed = True
        note = msg
        if is_default_data_dir(target):
            note += "\n（已使用默认目录）"
        QMessageBox.information(self, "已保存", note + "\n\n将重新加载数据。")
        self.accept()
