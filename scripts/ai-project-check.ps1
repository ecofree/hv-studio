[CmdletBinding()]
param(
    [switch]$Quick,
    [switch]$NoMedia,
    [ValidateSet('production','shared','legacy','all')][string]$Suite = 'production',
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot),
    [switch]$RequireReady,
    [switch]$RunGates,
    [switch]$AllowDirty,
    [string]$EvidencePath
)
$ErrorActionPreference = 'Stop'
$expectedRoot = [IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot)).TrimEnd('\')
if ([IO.Path]::GetFullPath($ProjectRoot).TrimEnd('\') -ne $expectedRoot) { throw 'ProjectRoot does not match this checker' }
$runtime = if ($env:HV_PYTHON) { $env:HV_PYTHON } else { Join-Path $ProjectRoot '.venv-core\Scripts\python.exe' }
& (Join-Path $ProjectRoot 'scripts\check-runtime-path.ps1') -ProjectRoot $ProjectRoot -RuntimePath $runtime
$env:PYTHONUTF8 = '1'
$env:PATH = (Join-Path $ProjectRoot 'tools\ffmpeg\bin') + [IO.Path]::PathSeparator + $env:PATH
$arguments = @((Join-Path $ProjectRoot 'bin\check_project.py'))
$executeTests = if ($PSBoundParameters.ContainsKey('RunGates')) { [bool]$RunGates } else { -not $Quick }
if ($executeTests) { $arguments += @('--tests', '--suite', $Suite) }
if ($NoMedia) { $arguments += '--no-media' }
if ($RequireReady) { $arguments += '--require-ready' }
if ($AllowDirty) { $arguments += '--allow-dirty' }
if ($EvidencePath) { $arguments += @('--output', [IO.Path]::GetFullPath($EvidencePath)) }
& $runtime @arguments
exit $LASTEXITCODE
