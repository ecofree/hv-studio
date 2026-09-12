param([string]$Python = 'python', [switch]$Mirror)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$asrEnvironment = Join-Path $projectRoot '.venv-asr'
$asrPython = Join-Path $asrEnvironment 'Scripts\python.exe'
if (-not (Test-Path -LiteralPath $asrPython)) {
    & $Python -m venv $asrEnvironment
    if ($LASTEXITCODE -ne 0) { throw 'ASR environment creation failed' }
}
& $asrPython -m pip install --only-binary=:all: -r (Join-Path $projectRoot 'requirements.asr.lock.txt')
if ($LASTEXITCODE -ne 0) { throw 'ASR dependency installation failed' }
& $asrPython -m pip check
if ($LASTEXITCODE -ne 0) { throw 'ASR dependency check failed' }
$fetchArguments = @((Join-Path $PSScriptRoot 'fetch_local_asr_model.py'), '--model-dir', (Join-Path $projectRoot '.cache\asr\faster-whisper-small'))
if ($Mirror) { $fetchArguments += '--mirror' }
& $asrPython @fetchArguments
if ($LASTEXITCODE -ne 0) { throw 'ASR model verification failed' }
