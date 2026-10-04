#!/usr/bin/env python3
"""本地插件管理器 — entry point.

Usage:
  python main.py
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import QApplication

from app import __app_name__, __version__
from app.ui.main_window import MainWindow
from app.utils.paths import app_icon_path, ensure_runtime_dirs, logs_root


def _setup_logging() -> None:
    log_dir = logs_root()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(log_dir / "app.log", encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )


def _apply_app_icon(app: QApplication) -> QIcon:
    icon = QIcon()
    path = app_icon_path()
    if path.is_file():
        icon = QIcon(str(path))
        app.setWindowIcon(icon)
    return icon


def main() -> int:
    ensure_runtime_dirs()
    _setup_logging()
    logging.getLogger(__name__).info("Starting %s v%s", __app_name__, __version__)

    # Windows taskbar grouping / icon association for pythonw
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "ruanjian.LocalPluginManager"
        )
    except Exception:
        pass

    app = QApplication(sys.argv)
    app.setApplicationName(__app_name__)
    app.setApplicationVersion(__version__)
    app.setStyle("Fusion")
    icon = _apply_app_icon(app)

    win = MainWindow()
    if not icon.isNull():
        win.setWindowIcon(icon)
    win.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
