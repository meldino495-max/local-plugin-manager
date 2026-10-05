from __future__ import annotations

from PyQt6.QtWidgets import QMessageBox, QWidget

PRIVACY_SUMMARY = (
    "本软件在本地运行，不会自动上传你的插件文件，也不会把网盘链接发给其他人。"
)

TRUST_SOURCE_WARNING = (
    "安全提醒：请仅使用你信任来源的压缩包或下载链接。"
    "确认后会把包内文件合并写入已安装的扩展目录；恶意包可能篡改扩展内容。"
)

PRIVACY_DETAIL = """隐私说明

1. 插件文件
   · 扫描、备份、更新都只在你的电脑本地完成
   · 软件不会自动把插件上传到任何服务器

2. Google Drive / OneDrive / 下载链接
   · 只有你主动粘贴链接并点击继续时，才会用该链接下载文件
   · 软件不会把链接分享、同步或发送给其他人
   · 链接仅可能保存在你本机的版本备注/缓存中
   · 仅允许 https://，并禁止访问本机/内网地址
   · 支持 OneDrive 短链（1drv.ms，含新版 /u/c/）与 Google Drive 分享链接
   · 网盘需设为「知道链接的任何人可查看」；请分享「文件」而不是文件夹
   · 公司 OneDrive/SharePoint 若租户禁止匿名下载，则可能仍无法拉取
   · 若链接可被任何人打开——这是网盘权限，不是本软件外传

3. 上传更新（信任来源）
   · 请仅安装你信任来源的压缩包或链接
   · 确认后会合并写入已安装扩展目录；路径穿越与体积有限制，但不会审查包内业务代码是否恶意
   · 执行前会自动备份，可在版本历史中回滚

4. 导出历史包
   · 只有你主动「导出历史包」并自行发送时，对方才会收到你勾选的版本压缩包

5. 数据存放
   · 历史、压缩包、日志保存在本机「缓存目录」
   · 可在工具栏「缓存目录」中查看和更改位置
"""


def show_privacy_dialog(parent: QWidget | None = None) -> None:
    box = QMessageBox(parent)
    box.setWindowTitle("隐私说明")
    box.setIcon(QMessageBox.Icon.Information)
    box.setText(PRIVACY_SUMMARY)
    box.setInformativeText(PRIVACY_DETAIL.strip())
    box.setStandardButtons(QMessageBox.StandardButton.Ok)
    box.exec()
