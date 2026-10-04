@echo off
setlocal
cd /d "%~dp0"
".venv\Scripts\python.exe" "%~dp0scripts\create_shortcut.py"
if errorlevel 1 (
  echo [ERROR] shortcut creation failed
  pause
  exit /b 1
)
echo.
echo Shortcut ready. You can copy LocalPluginManager.lnk to Desktop.
pause
endlocal
