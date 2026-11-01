@echo off
setlocal
title ClipSync - New Desktop
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start-desktop.ps1" %*
if errorlevel 1 (
    echo.
    echo ClipSync could not start. Please read the error above.
    pause
    exit /b 1
)
endlocal
