@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONUTF8=1
"%~dp0.venv-core\Scripts\python.exe" "%~dp0bin\studio_narrate.py" %*
exit /b %errorlevel%
