@echo off
title TradeBot
echo.
echo  =======================================
echo    TradeBot - Starting...
echo  =======================================
echo.

python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python not found.
    echo Please install Python 3.10+ from https://python.org
    echo Make sure to check "Add Python to PATH" during install.
    pause
    exit /b 1
)

echo Checking dependencies...
pip install -r requirements.txt -q 2>nul

echo.
echo  Open your browser to: http://localhost:5000
echo  Press Ctrl+C to stop TradeBot
echo.

python app.py
pause
