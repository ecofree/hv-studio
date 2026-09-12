@echo off
cd /d "%~dp0"
call "%~dp0runtime.bat"
if errorlevel 1 exit /b 1
"%HV_RUNTIME%" -u bin\serve.py %*
exit /b %errorlevel%
