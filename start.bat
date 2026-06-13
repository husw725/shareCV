@echo off
title ShareCV
echo ========================================
echo   ShareCV Quick Start (Windows)
echo ========================================
echo.

:: Check for Python
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Python not found! Please install Python 3.
    pause
    exit /b
)

:: Auto-install dependencies if anything is missing (also covers requirement changes)
python -c "import websockets, fastapi, httpx, uvicorn, pyperclip" >nul 2>&1
if %errorlevel% neq 0 (
    echo [*] Installing missing dependencies, please wait...
    python -m pip install -r requirements.txt
    echo.
)

:: Run ShareCV
python sharecv.py %*
if %errorlevel% neq 0 (
    echo.
    echo [ERROR] ShareCV exited with an error.
    echo Make sure dependencies are installed: pip install -r requirements.txt
    pause
)
