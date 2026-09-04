@echo off
REM hv CLI 启动脚本 (Windows)
cd /d %~dp0
C:\Users\coumoo\.workbuddy\binaries\python\envs\hv\Scripts\python.exe -m app.cli %*
