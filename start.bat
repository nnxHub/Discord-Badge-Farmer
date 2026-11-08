@echo off
chcp 65001 >nul
title RWPlugins - Discord Badge & Stream Farmer
cd /d "%~dp0"

where python >nul 2>&1
if %errorlevel% equ 0 (
    python streamer.py
    goto end
)

where py >nul 2>&1
if %errorlevel% equ 0 (
    py streamer.py
    goto end
)

echo.
echo ======================================================================
echo  [ERROR] Python is not installed or not added to PATH!
echo ======================================================================
echo  1. Download Python: https://www.python.org/downloads/
echo  2. IMPORTANT: Check the box "Add python.exe to PATH" during install!
echo  3. Restart this start.bat file after installing Python.
echo ======================================================================
echo.
pause
exit /b 1

:end
if %errorlevel% neq 0 (
    echo.
    echo Program exited with code: %errorlevel%
    pause
)