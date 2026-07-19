@echo off
setlocal EnableExtensions DisableDelayedExpansion
cd /d "%~dp0" || exit /b 1

set "PYTHON_EXE="
set "PYTHON_ARGS="
if exist ".venv\Scripts\python.exe" set "PYTHON_EXE=.venv\Scripts\python.exe"

if not defined PYTHON_EXE (
    where py >nul 2>nul
    if not errorlevel 1 (
        set "PYTHON_EXE=py"
        set "PYTHON_ARGS=-3"
    )
)

if not defined PYTHON_EXE (
    where python >nul 2>nul
    if not errorlevel 1 set "PYTHON_EXE=python"
)

if not defined PYTHON_EXE (
    echo Python 3.11 or newer was not found.
    exit /b 1
)

"%PYTHON_EXE%" %PYTHON_ARGS% -c "import sys; raise SystemExit(0 if sys.version_info ^>= (3, 11) else 1)" >nul 2>nul
if errorlevel 1 (
    echo Python 3.11 or newer is required.
    exit /b 1
)

if not exist "config.json" copy /Y "config.example.json" "config.json" >nul
"%PYTHON_EXE%" %PYTHON_ARGS% mp3_downloader.py --export-support
exit /b %ERRORLEVEL%
