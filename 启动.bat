@echo off
rem Launcher for the mouse macro tool.
rem NOTE: keep this file ASCII-only with CRLF line endings.
rem cmd.exe indexes batch files by byte offset; UTF-8 characters combined
rem with LF-only endings make it lose track of line boundaries.
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8

where python >nul 2>nul
if errorlevel 1 (
    echo.
    echo [!] Python was not found on PATH.
    echo     Install Python 3 from https://www.python.org/downloads/
    echo     and tick "Add python.exe to PATH" during setup.
    echo.
    pause
    exit /b 1
)

python mouse_macro.py
if errorlevel 1 (
    echo.
    echo [!] The program exited with an error ^(details above^).
    echo.
    pause
    exit /b 1
)
