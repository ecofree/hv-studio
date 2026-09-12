param([string]$Python = 'python', [string]$Wheelhouse)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$runtime = Join-Path $projectRoot '.venv-core\Scripts\python.exe'
& $Python -c "import sys; assert sys.version_info[:2] == (3,13), 'Python 3.13 required'"
if ($LASTEXITCODE -ne 0) { throw 'Wrong base Python version' }
if (-not (Test-Path -LiteralPath $runtime)) {
    & $Python -m venv (Join-Path $projectRoot '.venv-core')
    if ($LASTEXITCODE -ne 0) { throw 'Runtime creation failed' }
}
$installArgs = @('-m','pip','install','-r',(Join-Path $projectRoot 'requirements.core.lock.txt'))
if ($Wheelhouse) {
    $resolvedWheels = (Resolve-Path -LiteralPath $Wheelhouse).Path
    $manifest = Get-Content -LiteralPath (Join-Path $resolvedWheels 'manifest.json') -Raw | ConvertFrom-Json
    foreach ($wheel in $manifest) {
        if ([IO.Path]::GetFileName($wheel.wheel) -ne $wheel.wheel) { throw 'Invalid wheel path' }
        $actual = (Get-FileHash -LiteralPath (Join-Path $resolvedWheels $wheel.wheel) -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($actual -ne $wheel.sha256) { throw "Wheel hash mismatch: $($wheel.wheel)" }
    }
    $installArgs += @('--no-index','--find-links',$resolvedWheels)
}
& $runtime @installArgs
if ($LASTEXITCODE -ne 0) { throw 'Core dependency installation failed' }
& $runtime -m pip check
if ($LASTEXITCODE -ne 0) { throw 'Core dependency check failed' }
