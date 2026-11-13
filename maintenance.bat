@echo off
cd /d "%~dp0"
set PYTHONUTF8=1
set PATH=%~dp0tools\ffmpeg\bin;%PATH%
"%~dp0.venv-core\Scripts\python.exe" -m streamlit run "%~dp0app\webui\Main.py" --server.address 127.0.0.1 --server.port 8522
