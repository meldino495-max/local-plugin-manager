@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0.."

if not exist ".venv\Scripts\python.exe" (
  echo [INFO] Creating venv...
  python -m venv .venv
  if errorlevel 1 (
    echo [ERROR] Python not found.
    pause
    exit /b 1
  )
)

".venv\Scripts\python.exe" -m pip install -r requirements.txt -r requirements-build.txt
if errorlevel 1 (
  echo [ERROR] pip install failed.
  pause
  exit /b 1
)

".venv\Scripts\python.exe" scripts\build_exe.py
if errorlevel 1 (
  echo [ERROR] build failed.
  pause
  exit /b 1
)

echo.
echo Done: dist\本地插件管理器.exe
pause
endlocal
