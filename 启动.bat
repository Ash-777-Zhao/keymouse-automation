@echo off
rem Launcher for the mouse macro tool.
rem
rem NOTE: keep this file ASCII-only with CRLF line endings.
rem cmd.exe indexes batch files by byte offset; UTF-8 characters combined
rem with LF-only endings make it lose track of line boundaries, and the
rem lines get chopped together into garbage commands.

cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8

rem Use pythonw.exe, the windowless interpreter, so no console window
rem sticks around. It ships with Python next to python.exe.
rem If startup fails, the program writes error.log and shows a message box.
set "PYW="
for /f "delims=" %%I in ('where pythonw.exe 2^>nul') do if not defined PYW set "PYW=%%I"

if not defined PYW (
    echo.
    echo [!] pythonw.exe was not found on PATH.
    echo     It comes with Python. Re-run the Python installer and tick
    echo     "Add python.exe to PATH", then try again.
    echo.
    pause
    exit /b 1
)

if not exist "mouse_macro.py" (
    echo.
    echo [!] mouse_macro.py is not next to this file.
    echo.
    pause
    exit /b 1
)

rem "start" launches it detached, so this console can close right away.
start "" "%PYW%" "%~dp0mouse_macro.py"
exit /b 0
