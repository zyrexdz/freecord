@echo off
cd /d "%~dp0"
title FreeCord
color 0B
cls

set "PY_CMD="
python --version >nul 2>&1
if %errorlevel% equ 0 (
    set "PY_CMD=python"
) else (
    py --version >nul 2>&1
    if %errorlevel% equ 0 (
        set "PY_CMD=py"
    ) else (
        python3 --version >nul 2>&1
        if %errorlevel% equ 0 (
            set "PY_CMD=python3"
        )
    )
)

if "%PY_CMD%"=="" (
    echo [ERROR] Python was not found on your system.
    echo Please install Python 3.10+ from https://www.python.org/downloads/
    echo IMPORTANT: Make sure to check "Add Python to PATH" during installation.
    echo.
    pause
    exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
    %PY_CMD% -m venv .venv >nul 2>&1
)

if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] Failed to create virtual environment.
    echo Try running as Administrator.
    pause
    exit /b 1
)

.venv\Scripts\python.exe -m pip install -r requirements.txt --quiet >nul 2>&1
if errorlevel 1 (
    .venv\Scripts\python.exe -m pip install -r requirements.txt --quiet >nul 2>&1
)

if not exist "data" mkdir data
if not exist "backups" mkdir backups

.venv\Scripts\python.exe main.py %*

if errorlevel 1 (
    echo.
    pause
)
