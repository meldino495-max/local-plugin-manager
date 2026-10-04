@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo [INFO] First run: creating venv and installing dependencies...
  python -m venv .venv
  if errorlevel 1 (
    echo [ERROR] Python not found. Install Python 3.10+ and check Add to PATH.
    pause
    exit /b 1
  )
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt
  if errorlevel 1 (
    echo [ERROR] pip install failed.
    pause
    exit /b 1
  )
)

if exist ".venv\Scripts\pythonw.exe" (
  start "" ".venv\Scripts\pythonw.exe" "%~dp0main.py"
) else (
  start "" ".venv\Scripts\python.exe" "%~dp0main.py"
)
endlocal
