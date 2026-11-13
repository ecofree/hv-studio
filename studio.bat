@echo off
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONUNBUFFERED=1
call "%~dp0runtime.bat"
if errorlevel 1 exit /b 1
"%HV_RUNTIME%" -m streamlit run "%~dp0app\webui\Studio.py" --server.address 127.0.0.1 --server.port 8521
exit /b %errorlevel%
