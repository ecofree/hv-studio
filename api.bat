@echo off
REM hv API 启动脚本 (Windows)
chcp 65001 >nul
set PYTHONUTF8=1
cd /d "%~dp0"
set PYTHONUNBUFFERED=1
call "%~dp0runtime.bat"
if errorlevel 1 exit /b 1
"%HV_RUNTIME%" -u -m app.cli api %*
exit /b %errorlevel%
