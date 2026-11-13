$ErrorActionPreference='Stop'
$root=Split-Path -Parent $PSScriptRoot
$env:PYTHONUTF8='1'
$env:PATH=(Join-Path $root 'tools\ffmpeg\bin')+';'+$env:PATH
Set-Location -LiteralPath $root
New-Item -ItemType Directory -Force -Path (Join-Path $root 'workspace\state\acceptance') | Out-Null
& (Join-Path $root '.venv-core\Scripts\python.exe') -m pytest -q -m production tests/test_studio_workflow.py tests/test_studio_voice.py tests/test_narration_leveling.py tests/test_local_voice_direction.py tests/test_overlay_author.py tests/test_production_entrypoint.py
$code=$LASTEXITCODE
@{exit_code=$code;finished=(Get-Date).ToString('o')} | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $root 'workspace\state\acceptance\studio-targeted-result.json') -Encoding UTF8
exit $code
