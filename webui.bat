@echo off
REM hv WebUI 启动脚本 (Windows)
cd /d %~dp0
REM -u 无缓冲：WebUI 里触发的长任务同样需要实时看到进度/ETA 心跳
set PYTHONUNBUFFERED=1
set PYTHONUTF8=1
C:\Users\coumoo\.workbuddy\binaries\python\envs\hv\Scripts\python.exe -u -m app.cli webui %*
