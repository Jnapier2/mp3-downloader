@echo off
REM Copyright © 2026 Gateway Information Group LLC. All rights reserved.
setlocal EnableExtensions DisableDelayedExpansion
cd /d "%~dp0" || (
    echo ERROR: The MP3 Downloader project folder could not be opened.
    exit /b 2
)

set "MP3_SCRIPT=%~dp0mp3_downloader.py"
if not exist "%MP3_SCRIPT%" (
    echo ERROR: mp3_downloader.py is missing beside this launcher.
    exit /b 2
)

set "PYTHON_EXE="
set "PYTHON_ARGS="

if exist "%~dp0.venv\Scripts\python.exe" (
    "%~dp0.venv\Scripts\python.exe" -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>&1
    if not errorlevel 1 set "PYTHON_EXE=%~dp0.venv\Scripts\python.exe"
)

if not defined PYTHON_EXE (
    where py.exe >nul 2>&1
    if not errorlevel 1 (
        py.exe -3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>&1
        if not errorlevel 1 (
            set "PYTHON_EXE=py.exe"
            set "PYTHON_ARGS=-3"
        )
    )
)

if not defined PYTHON_EXE (
    where python.exe >nul 2>&1
    if not errorlevel 1 (
        python.exe -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>&1
        if not errorlevel 1 set "PYTHON_EXE=python.exe"
    )
)

if not defined PYTHON_EXE (
    echo ERROR: Python 3.11 or newer was not found.
    echo See README.md for setup instructions.
    exit /b 3
)

"%PYTHON_EXE%" %PYTHON_ARGS% -c "import certifi, yt_dlp" >nul 2>&1
if errorlevel 1 (
    echo ERROR: The pinned project dependencies are not installed for the selected Python runtime.
    echo Run: "%PYTHON_EXE%" %PYTHON_ARGS% -m pip install --require-hashes --only-binary=:all: -r requirements.txt
    exit /b 4
)

if not "%~1"=="" (
    "%PYTHON_EXE%" %PYTHON_ARGS% "%MP3_SCRIPT%" %*
    exit /b %errorlevel%
)

if not exist "%~dp0config.json" copy /Y "%~dp0config.example.json" "%~dp0config.json" >nul
echo MP3 Downloader 1.0.0
echo Download only media you own or are authorized to save.
"%PYTHON_EXE%" %PYTHON_ARGS% "%MP3_SCRIPT%"
exit /b %errorlevel%
