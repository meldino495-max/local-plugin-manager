from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
)

from app.core.history_transfer import TransferItemPreview
from app.ui.privacy import PRIVACY_SUMMARY


class ExportHistoryDialog(QDialog):
    def __init__(self, items: list[TransferItemPreview], parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("导出历史包")
        self.resize(640, 460)
        self._items = items
        self.selected_uids: list[str] = []

        layout = QVBoxLayout(self)
        layout.addWidget(
            QLabel(
                "选择要分享的插件历史。将打包各版本的压缩包，对方导入后可在「版本历史」里再导出 ZIP/7z。"
            )
        )
        notice = QLabel(
            f"{PRIVACY_SUMMARY}\n"
            "注意：只有你主动导出并自行发送这个历史包时，对方才会收到其中的版本压缩包。"
        )
        notice.setWordWrap(True)
        notice.setStyleSheet(
            "color:#255; background:#eef6f2; border:1px solid #b7d0c4;"
            " border-radius:4px; padding:8px;"
        )
        layout.addWidget(notice)

        self.list = QListWidget()
        for it in items:
            text = (
                f"{it.name}  v{it.version or '?'}  "
                f"（历史 {it.history_count} 条 / 可打包 {it.package_count} 个）"
            )
            item = QListWidgetItem(text)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked)
            item.setData(Qt.ItemDataRole.UserRole, it.uid)
            item.setToolTip(it.path)
            self.list.addItem(item)
        layout.addWidget(self.list, 1)

        row = QHBoxLayout()
        btn_all = QPushButton("全选")
        btn_none = QPushButton("全不选")
        btn_all.clicked.connect(self._check_all)
        btn_none.clicked.connect(self._uncheck_all)
        row.addWidget(btn_all)
        row.addWidget(btn_none)
        row.addStretch(1)
        layout.addLayout(row)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("导出")
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _check_all(self) -> None:
        for i in range(self.list.count()):
            self.list.item(i).setCheckState(Qt.CheckState.Checked)

    def _uncheck_all(self) -> None:
        for i in range(self.list.count()):
            self.list.item(i).setCheckState(Qt.CheckState.Unchecked)

    def _accept(self) -> None:
        uids: list[str] = []
        for i in range(self.list.count()):
            item = self.list.item(i)
            if item.checkState() == Qt.CheckState.Checked:
                uids.append(item.data(Qt.ItemDataRole.UserRole))
        if not uids:
            return
        self.selected_uids = uids
        self.accept()
