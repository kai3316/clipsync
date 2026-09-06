@echo off
REM ============================================================
REM  ClipSync - one-click Windows build
REM
REM  Double-click this file, or from a terminal:
REM      build.bat                 build with what's installed
REM      build.bat -Deps           refresh dependencies first
REM      build.bat -Zip            also make dist\clipsync-windows.zip
REM      build.bat -Run            launch the .exe afterwards
REM      build.bat -StopRunning    kill a running clipsync.exe first
REM      build.bat -KeepBuild      keep the build\ intermediates
REM
REM  Result: dist\clipsync.exe  (intermediates are deleted automatically)
REM  All the real work lives in scripts\build_exe.ps1
REM ============================================================

setlocal EnableExtensions

set "PS1=%~dp0scripts\build_exe.ps1"

if not exist "%PS1%" (
    echo [ERROR] Cannot find "%PS1%"
    set "RC=1"
    goto :finish
)

REM -ExecutionPolicy Bypass so this works on a default Windows box without
REM the user having to loosen their machine-wide policy.
powershell -NoProfile -ExecutionPolicy Bypass -File "%PS1%" %*
set "RC=%ERRORLEVEL%"

:finish
REM Pause only when double-clicked, so the results stay readable instead of the
REM window vanishing. When launched from Explorer, %cmdcmdline% contains this
REM script's name; from an already-open console it is just the bare cmd.exe path.
REM
REM Caveat: invoking this .bat from PowerShell or a CI runner also spawns
REM `cmd /c ...build.bat`, which trips the same check. Set CLIPSYNC_NOPAUSE=1
REM for unattended use, or call scripts\build_exe.ps1 directly.
if defined CLIPSYNC_NOPAUSE goto :done
echo %cmdcmdline% | find /i "%~nx0" >nul
if not errorlevel 1 (
    echo.
    pause
)

:done

exit /b %RC%
