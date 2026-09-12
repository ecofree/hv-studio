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
    # Launch through Windows' process broker, outside the calling terminal's job
    # object. pythonw has no visible console. Closing a tool session must not kill
    # production. The supervisor's kernel lock still rejects duplicate workers.
    $launch = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{
        CommandLine = '"' + $workerPython + '" ' + $workerArguments
        CurrentDirectory = $workerRoot
    }
    if ($launch.ReturnValue -ne 0) { throw "Windows process broker rejected launch: $($launch.ReturnValue)" }
    $startedPid = $launch.ProcessId
}
@{startup_shortcut=$shortcutPath; runtime=$workerPython; start_pid=$startedPid;
  startup_scope='current user sign-in'; start_method='Windows process broker'; boot_without_sign_in=$false} | ConvertTo-Json
