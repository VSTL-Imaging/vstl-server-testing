@echo off
setlocal
cd /d "%~dp0\.."

set "MSG=%*"
if not defined MSG set "MSG=Update VSTL testing server project"

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0push-to-github.ps1" -TestingServer -Message "%MSG%"
if errorlevel 1 (
  echo.
  echo GitHub push failed.
  pause
  exit /b 1
)

echo.
echo GitHub push completed.
pause
