param([switch]$Quick)
$projectRoot = Split-Path -Parent $PSScriptRoot
$runtime = if ($env:HV_PYTHON) { $env:HV_PYTHON } else { Join-Path $projectRoot '.venv-core\Scripts\python.exe' }
$env:PYTHONUTF8 = '1'
$arguments = @((Join-Path $projectRoot 'bin\check_project.py'))
if (-not $Quick) { $arguments += '--tests' }
& $runtime @arguments
exit $LASTEXITCODE
