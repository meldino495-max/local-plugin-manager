# 本地插件管理器

扫描本机浏览器扩展，支持置顶 / 分类，并通过 `7z` / `7zip` / `zip` 安全更新扩展文件，同时保留每个版本以便回滚、导出或升到最新。

## 支持的浏览器

- Google Chrome
- Microsoft Edge
- Brave
- Firefox（解压后的扩展文件夹；纯 `.xpi` 仅展示，不直接覆盖）

## 功能

1. **自动扫描**：列出扩展名称、版本号、浏览器、配置文件、完整路径
2. **置顶 / 分类**：本地记住，可筛选
3. **上传更新**：选择本地压缩包，或填写 Google Drive / 其它下载链接 → 自动下载 → 解压校验（必须含 `manifest.json`）→ 预览 → **自动备份** → 覆盖写入
4. **版本历史**：每次更新前备份 + 保存上传包；可恢复任意旧版本，或一键恢复最新上传包
5. **导出版本**：历史中任一版本可导出为 `zip` / `7z`，也可导出当前安装目录
6. **导出/导入历史包**：把多个插件的版本历史与压缩包打成 ZIP 发给别人；对方导入后可在「版本历史」里再导出 ZIP/7z

## 安全策略

- 拒绝压缩包内的 `../` 路径穿越（含 Windows 盘符逃逸）
- 写入范围严格限制在该扩展目录内
- 更新前强制快照当前文件到 `data/versions/`
- 上传的压缩包副本保存在 `data/archives/`
- **合并覆盖**：只替换/新增压缩包里的文件，不删除目标里多出来的旧文件
- **下载链接仅允许 HTTPS**，并禁止访问本机/内网地址（防 SSRF）；支持 Google Drive / OneDrive 公开文件分享链接
- 软件不会上传你的插件或外传网盘链接（见应用内「隐私说明」）

## 安装与运行

### 下载 Windows 可执行文件（推荐）

1. 打开 GitHub 仓库的 [Releases](https://github.com/meldino495-max/local-plugin-manager/releases)
2. 下载 `LocalPluginManager-v*-windows-x64.exe`（GitHub Release 资源名须为 ASCII；本地打包产物仍是 `本地插件管理器.exe`）
3. 放到任意文件夹后双击运行（首次会在同目录创建 `data/` 与 `app_config.json`）

发版方式（维护者）：推送版本标签即可自动打包并上传 Release，例如：

```bash
git tag v1.1.0
git push origin v1.1.0
```

本地手动打包：

```bash
# 双击 scripts\build_exe.bat
# 或：
pip install -r requirements.txt -r requirements-build.txt
python scripts/build_exe.py
# 产物：dist\本地插件管理器.exe
```

### 从源码运行

**最简单**：双击 `打开.bat`（首次会自动创建虚拟环境并安装依赖）。

图标：窗口与任务栏使用 `resources/icon.ico`。  
若要在桌面/资源管理器显示图标：双击 `创建快捷方式.bat`，使用生成的 `LocalPluginManager.lnk`（或 `..\LocalPluginManager\Local Plugin Manager.lnk`）。

或手动：

```bash
cd 本地插件管理器
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python main.py
```

## 更新后

浏览器可能仍缓存旧代码。请到 `chrome://extensions` / `edge://extensions` 打开开发者模式，对该扩展点击「重新加载」。

## 数据 / 缓存位置

默认在软件目录下的 `data/`。可在工具栏 **「缓存目录」** 自定义到其它磁盘。

配置文件 `app_config.json`（与软件同目录）记录自定义路径。

| 路径 | 内容 |
|------|------|
| `store.json` | 置顶、分类、版本历史索引 |
| `versions/` | 每次更新前的目录快照 |
| `archives/` | 上传/下载的 7z/zip 副本 |
| `temp/` | 临时解压与下载 |
| `logs/` | 运行日志 |
