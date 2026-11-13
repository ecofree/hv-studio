$ErrorActionPreference='Stop'
$root=Split-Path -Parent $PSScriptRoot
$env:PYTHONUTF8='1'
$env:PATH=(Join-Path $root 'tools\ffmpeg\bin')+';'+$env:PATH
Set-Location -LiteralPath $root
& (Join-Path $root '.venv-core\Scripts\python.exe') -m pytest -q -m production tests/test_studio_ui.py tests/test_studio_workflow.py tests/test_studio_voice.py tests/test_existing_images.py tests/test_overlay_author.py tests/test_production_entrypoint.py::test_public_entry_renders_and_verifies_without_touching_legacy tests/test_script_handoff.py
$code=$LASTEXITCODE
@{exit_code=$code;finished=(Get-Date).ToString('o')} | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $root 'workspace\state\acceptance\studio-integration-result.json') -Encoding UTF8
exit $code
