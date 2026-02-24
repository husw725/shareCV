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

:: Run ShareCV
python sharecv.py %*
if %errorlevel% neq 0 (
    echo.
    echo [ERROR] ShareCV exited with an error.
    echo Make sure dependencies are installed: pip install -r requirements.txt
    pause
)
