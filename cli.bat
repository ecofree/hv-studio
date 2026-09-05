@echo off
REM hv CLI 启动脚本 (Windows)
chcp 65001 >nul
set PYTHONUTF8=1
cd /d %~dp0
REM -u: 无缓冲 stdout。长任务（300 张生图 / 20min 合成）若无 -u，
REM     Windows 下 print 全缓冲在管道里，进度与 ETA 心跳一条都看不到，
REM     看起来像卡死。PYTHONUNBUFFERED=1 兜底（子进程同样生效）。
set PYTHONUNBUFFERED=1
C:\Users\coumoo\.workbuddy\binaries\python\envs\hv\Scripts\python.exe -u -m app.cli %*
