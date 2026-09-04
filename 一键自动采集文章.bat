@echo off
chcp 65001 >nul
title 历史第一狗仔队 - 全自动文章采集入库器
cd /d "D:\hv"
"C:\Users\coumoo\.workbuddy\binaries\python\envs\hv\Scripts\python.exe" "D:\hv\bin\auto_collector.py"
pause
