[CmdletBinding()]
param([Parameter(Mandatory=$true)][string]$ProjectRoot,
      [Parameter(Mandatory=$true)][string]$RuntimePath)
$ErrorActionPreference = 'Stop'
$candidate = [IO.Path]::GetFullPath($RuntimePath).TrimEnd('\','/')
$marker = Join-Path $ProjectRoot '.studio-project.json'
if (Test-Path -LiteralPath $marker) {
    $settings = Get-Content -LiteralPath $marker -Raw -Encoding UTF8 | ConvertFrom-Json
    $retiredRoots = @($settings.legacy_path_roots)
    if ($settings.original_root) { $retiredRoots += $settings.original_root }
    foreach ($retiredRoot in $retiredRoots) {
        if (-not $retiredRoot) { continue }
        $prefix = [IO.Path]::GetFullPath($retiredRoot).TrimEnd('\','/')
        if ($candidate.Equals($prefix, [StringComparison]::OrdinalIgnoreCase) -or
            $candidate.StartsWith($prefix + '\', [StringComparison]::OrdinalIgnoreCase)) {
            throw 'Runtime points to a retired checkout. Use this project runtime.'
        }
    }
}
