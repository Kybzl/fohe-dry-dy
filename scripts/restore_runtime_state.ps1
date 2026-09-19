[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$SnapshotRoot,

    [Parameter(Mandatory = $false)]
    [switch]$Apply,

    [Parameter(Mandatory = $false)]
    [switch]$AllowSystemDrive
)

$ErrorActionPreference = "Stop"
$projectRoot = [System.IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
$snapshot = [System.IO.Path]::GetFullPath($SnapshotRoot)
$systemRoot = [System.IO.Path]::GetPathRoot($env:SystemRoot)
$projectDrive = [System.IO.Path]::GetPathRoot($projectRoot)

if (-not $AllowSystemDrive -and $projectDrive -ieq $systemRoot) {
    throw "Project is on the system drive ($systemRoot). Move it to another drive, or explicitly use -AllowSystemDrive."
}
if (-not (Test-Path -LiteralPath $snapshot -PathType Container)) {
    throw "Snapshot directory does not exist: $snapshot"
}

$manifestPath = Join-Path $snapshot "manifest.json"
if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
    throw "Snapshot manifest is missing: $manifestPath"
}
$manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
if ([int]$manifest.format_version -ne 1 -or $manifest.project -ne "fohe-dy") {
    throw "Unsupported or unrelated runtime-state snapshot."
}

$allowedPaths = @(
    ".env",
    "config.yaml",
    "data/library.db"
)
$validated = @()
foreach ($entry in $manifest.files) {
    $relative = ([string]$entry.path).Replace("/", [System.IO.Path]::DirectorySeparatorChar)
    $allowed = $allowedPaths -contains ([string]$entry.path)
    if (-not $allowed -and -not ([string]$entry.path).StartsWith("browser_data/")) {
        throw "Manifest contains a path outside the runtime-state allowlist: $($entry.path)"
    }
    $source = [System.IO.Path]::GetFullPath((Join-Path $snapshot $relative))
    $snapshotPrefix = $snapshot.TrimEnd('\', '/') + [System.IO.Path]::DirectorySeparatorChar
    if (-not $source.StartsWith($snapshotPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Manifest path escapes the snapshot directory: $($entry.path)"
    }
    if (-not (Test-Path -LiteralPath $source -PathType Leaf)) {
        throw "Snapshot file is missing: $($entry.path)"
    }
    $file = Get-Item -LiteralPath $source
    if ([long]$file.Length -ne [long]$entry.size) {
        throw "Snapshot size mismatch: $($entry.path)"
    }
    $actualHash = (Get-FileHash -LiteralPath $source -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actualHash -ne ([string]$entry.sha256).ToLowerInvariant()) {
        throw "Snapshot SHA-256 mismatch: $($entry.path)"
    }
    $validated += [pscustomobject]@{
        Source = $source
        Relative = $relative
        Target = Join-Path $projectRoot $relative
    }
}

foreach ($required in @(".env", "config.yaml", "data/library.db")) {
    if (-not ($manifest.files.path -contains $required)) {
        throw "Required snapshot entry is missing: $required"
    }
}

Write-Host "Runtime-state restore plan (hashes verified):"
Write-Host "  Snapshot: $snapshot"
Write-Host "  Project:  $projectRoot"
Write-Host "  Files:    $($validated.Count)"
Write-Host "  Contains browser profile: $([bool]$manifest.includes_browser_profile)"
if (-not $Apply) {
    Write-Host "Preview only: no files were changed. Add -Apply to restore."
    exit 0
}

$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$rollbackRoot = Join-Path $projectRoot "data\restore-rollbacks\$stamp"
New-Item -ItemType Directory -Force -Path $rollbackRoot | Out-Null

# Back up current single-file state before overwriting it.
foreach ($relative in @(".env", "config.yaml", "data\library.db")) {
    $current = Join-Path $projectRoot $relative
    if (Test-Path -LiteralPath $current -PathType Leaf) {
        $rollback = Join-Path $rollbackRoot $relative
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $rollback) | Out-Null
        Copy-Item -LiteralPath $current -Destination $rollback
    }
}

# A browser profile is a directory and may be large. Move the current profile
# into the rollback area instead of duplicating it before copying the snapshot.
$browserTarget = Join-Path $projectRoot "browser_data"
if ([bool]$manifest.includes_browser_profile -and (Test-Path -LiteralPath $browserTarget)) {
    $browserRollback = Join-Path $rollbackRoot "browser_data"
    Move-Item -LiteralPath $browserTarget -Destination $browserRollback
}

foreach ($item in $validated) {
    $parent = Split-Path -Parent $item.Target
    New-Item -ItemType Directory -Force -Path $parent | Out-Null
    Copy-Item -LiteralPath $item.Source -Destination $item.Target -Force
}

Write-Host "Runtime-state restore completed."
Write-Host "Rollback copy: $rollbackRoot"
Write-Host "Run: .\.venv\python.exe app.py --check"
Write-Host "Run: .\.venv\python.exe app.py --check-library"
