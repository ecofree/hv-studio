@echo off
set "HV_RUNTIME=%HV_PYTHON%"
if not defined HV_RUNTIME set "HV_RUNTIME=%~dp0.venv-core\Scripts\python.exe"
if not exist "%HV_RUNTIME%" (
  echo Core runtime missing. Run scripts\bootstrap-core.ps1 first, or set HV_PYTHON.
  exit /b 1
)
set PYTHONUTF8=1
set PYTHONUNBUFFERED=1
set "PATH=%~dp0tools\ffmpeg\bin;%PATH%"
exit /b 0
