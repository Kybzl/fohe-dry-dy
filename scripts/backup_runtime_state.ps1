[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$DestinationRoot,

    [Parameter(Mandatory = $false)]
    [switch]$ExcludeBrowserProfile,

    [Parameter(Mandatory = $false)]
    [switch]$DryRun,

    [Parameter(Mandatory = $false)]
    [switch]$AllowSystemDrive
)

$ErrorActionPreference = "Stop"
$projectRoot = [System.IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
$destination = [System.IO.Path]::GetFullPath($DestinationRoot)
$systemRoot = [System.IO.Path]::GetPathRoot($env:SystemRoot)
$destinationDrive = [System.IO.Path]::GetPathRoot($destination)

if (-not $AllowSystemDrive -and $destinationDrive -ieq $systemRoot) {
    throw "Backup destination is on the system drive ($systemRoot). Choose another drive, or explicitly use -AllowSystemDrive."
}
$projectPrefix = $projectRoot.TrimEnd('\', '/') + [System.IO.Path]::DirectorySeparatorChar
if (
    $destination -ieq $projectRoot -or
    $destination.StartsWith($projectPrefix, [System.StringComparison]::OrdinalIgnoreCase)
) {
    throw "Backup destination must be outside the project directory."
}

$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$snapshotName = "fohe-dy-runtime-$stamp"
$snapshotRoot = Join-Path $destination $snapshotName
$items = @(
    [pscustomobject]@{ Source = (Join-Path $projectRoot ".env"); Relative = ".env"; Required = $true }
    [pscustomobject]@{ Source = (Join-Path $projectRoot "data\library.db"); Relative = "data\library.db"; Required = $true }
    [pscustomobject]@{ Source = (Join-Path $projectRoot "config.yaml"); Relative = "config.yaml"; Required = $true }
)
if (-not $ExcludeBrowserProfile) {
    $items += [pscustomobject]@{
        Source = (Join-Path $projectRoot "browser_data")
        Relative = "browser_data"
        Required = $false
    }
}

$missing = @($items | Where-Object { $_.Required -and -not (Test-Path -LiteralPath $_.Source) })
if ($missing.Count -gt 0) {
    throw "Required runtime state is missing: $($missing.Source -join ', ')"
}

Write-Host "Runtime-state snapshot plan:"
Write-Host "  Project:     $projectRoot"
Write-Host "  Destination: $snapshotRoot"
foreach ($item in $items) {
    $state = if (Test-Path -LiteralPath $item.Source) { "include" } else { "skip (missing)" }
    Write-Host "  [$state] $($item.Source) -> $($item.Relative)"
}
if ($DryRun) {
    Write-Host "Dry run only: no files were copied."
    exit 0
}

New-Item -ItemType Directory -Force -Path $snapshotRoot | Out-Null
foreach ($item in $items) {
    if (-not (Test-Path -LiteralPath $item.Source)) {
        continue
    }
    $target = Join-Path $snapshotRoot $item.Relative
    $targetParent = Split-Path -Parent $target
    New-Item -ItemType Directory -Force -Path $targetParent | Out-Null
    if (Test-Path -LiteralPath $item.Source -PathType Container) {
        Copy-Item -LiteralPath $item.Source -Destination $target -Recurse
    } else {
        Copy-Item -LiteralPath $item.Source -Destination $target
    }
}

$manifestEntries = @()
$files = Get-ChildItem -LiteralPath $snapshotRoot -File -Recurse | Sort-Object FullName
foreach ($file in $files) {
    # Windows PowerShell 5.1 runs on a .NET version without Path.GetRelativePath.
    # Both paths are already absolute, so a checked prefix subtraction is portable.
    $snapshotPrefix = $snapshotRoot.TrimEnd('\', '/') + [System.IO.Path]::DirectorySeparatorChar
    if (-not $file.FullName.StartsWith($snapshotPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Snapshot file escaped the destination root: $($file.FullName)"
    }
    $relative = $file.FullName.Substring($snapshotPrefix.Length)
    $hash = Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256
    $manifestEntries += [ordered]@{
        path = $relative.Replace("\", "/")
        size = $file.Length
        sha256 = $hash.Hash.ToLowerInvariant()
    }
}
$manifest = [ordered]@{
    format_version = 1
    created_at = (Get-Date).ToUniversalTime().ToString("o")
    project = "fohe-dy"
    source_project = $projectRoot
    includes_browser_profile = (-not $ExcludeBrowserProfile)
    file_count = $manifestEntries.Count
    files = $manifestEntries
}
$manifestPath = Join-Path $snapshotRoot "manifest.json"
$manifest | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $manifestPath -Encoding UTF8

Write-Host "Runtime-state snapshot completed."
Write-Host "Snapshot: $snapshotRoot"
Write-Host "Files:    $($manifestEntries.Count)"
Write-Host "Manifest: $manifestPath"
Write-Host "The snapshot contains secrets and browser login state. Keep it private."
