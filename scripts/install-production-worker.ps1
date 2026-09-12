[CmdletBinding()]
param([switch]$StartNow)
$ErrorActionPreference = 'Stop'
$workerRoot = Split-Path -Parent $PSScriptRoot
$workerPython = Join-Path $workerRoot '.venv-core\Scripts\pythonw.exe'
$workerScript = Join-Path $workerRoot 'bin\serve.py'
if (-not (Test-Path -LiteralPath $workerPython -PathType Leaf)) { throw 'Production runtime is missing' }
$workerArguments = '"' + $workerScript + '" --worker --state-dir state/worker-service'
$startupDirectory = [Environment]::GetFolderPath('Startup')
$shortcutPath = Join-Path $startupDirectory 'HV Production Worker.lnk'
$shortcutShell = New-Object -ComObject WScript.Shell
$shortcut = $shortcutShell.CreateShortcut($shortcutPath)
if ((Test-Path -LiteralPath $shortcutPath) -and $shortcut.TargetPath -ne $workerPython) {
    throw 'A different startup shortcut already uses this name'
}
$shortcut.TargetPath = $workerPython
$shortcut.Arguments = $workerArguments
$shortcut.WorkingDirectory = $workerRoot
$shortcut.WindowStyle = 7
$shortcut.Description = 'Resume explicitly budgeted HV queue after this user signs in. Never enqueue or approve content.'
$shortcut.Save()
$serviceState = Join-Path $workerRoot 'state\worker-service'
New-Item -ItemType Directory -Path $serviceState -Force | Out-Null
if ($StartNow) {
    if (Test-Path -LiteralPath (Join-Path $serviceState 'stop')) { throw 'Operator stop is present' }
    if (Test-Path -LiteralPath (Join-Path $workerRoot 'state\production-queue\STOP')) { throw 'Queue STOP is present' }
    # The supervisor owns a kernel lock; duplicate starts cannot create workers.
    $workerProcess = Start-Process -FilePath $workerPython -ArgumentList $workerArguments -WorkingDirectory $workerRoot -WindowStyle Hidden -PassThru
    $startedPid = $workerProcess.Id
}
@{startup_shortcut=$shortcutPath; runtime=$workerPython; start_pid=$startedPid;
  startup_scope='current user sign-in'; boot_without_sign_in=$false} | ConvertTo-Json
