@echo off
REM Copyright © 2026 Gateway Information Group LLC. All rights reserved.
setlocal EnableExtensions DisableDelayedExpansion
call "%~dp0run_mp3_downloader.bat" --export-support %*
exit /b %errorlevel%
