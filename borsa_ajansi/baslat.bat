@echo off
REM Borsa Ajansi Telegram botunu baslatir (pencere acik kaldigi surece calisir)
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
.venv\Scripts\python.exe main.py bot
pause
