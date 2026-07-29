@echo off
wpeinit
echo.
echo Starting VSTL Windows QC Helper...
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File X:\VSTL-QC\Start-VstlQc.ps1
echo.
echo VSTL Windows QC Helper exited.
cmd.exe
