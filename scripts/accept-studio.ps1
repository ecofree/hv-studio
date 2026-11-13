$ErrorActionPreference='Stop'
$root=Split-Path -Parent $PSScriptRoot
$env:PYTHONUTF8='1'
$env:PATH=(Join-Path $root 'tools\ffmpeg\bin')+';'+$env:PATH
Set-Location -LiteralPath $root
& powershell.exe -NoProfile -File (Join-Path $root 'scripts\ai-project-check.ps1') -ProjectRoot $root -EvidencePath (Join-Path $root 'workspace\state\acceptance\production.json')
$code=$LASTEXITCODE
@{exit_code=$code;finished=(Get-Date).ToString('o')} | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $root 'workspace\state\acceptance\production-result.json') -Encoding UTF8
exit $code
