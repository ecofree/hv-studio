@echo off
REM hv API 启动脚本 (Windows)
chcp 65001 >nul
set PYTHONUTF8=1
cd /d %~dp0
C:\Users\coumoo\.workbuddy\binaries\python\envs\hv\Scripts\python.exe -m app.cli api %*
