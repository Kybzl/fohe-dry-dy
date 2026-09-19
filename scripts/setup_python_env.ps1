[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$environmentPath = Join-Path $projectRoot ".venv"
$pythonPath = Join-Path $environmentPath "python.exe"
$requirementsPath = Join-Path $projectRoot "requirements.txt"

if (-not (Get-Command conda -ErrorAction SilentlyContinue)) {
    throw "conda is required. Install Miniconda/Anaconda, then run this script again."
}

$environmentReady = $false
if (Test-Path -LiteralPath $pythonPath) {
    & $pythonPath -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 12) else 1)"
    $environmentReady = ($LASTEXITCODE -eq 0)
}

if (-not $environmentReady -and (Test-Path -LiteralPath $environmentPath)) {
    $stamp = Get-Date -Format "yyyyMMdd-HHmmss"
    $backupPath = Join-Path $projectRoot ".venv-incompatible-$stamp"
    Move-Item -LiteralPath $environmentPath -Destination $backupPath
    Write-Host "Preserved incompatible environment at $backupPath"
}

if (-not $environmentReady) {
    conda create --prefix $environmentPath --override-channels -c conda-forge python=3.12 pip -y
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to create the Python 3.12 environment."
    }
}

conda install --prefix $environmentPath --override-channels -c conda-forge ffmpeg -y
if ($LASTEXITCODE -ne 0) {
    throw "Failed to install FFmpeg into the project environment."
}

& $pythonPath -m pip install -r $requirementsPath
if ($LASTEXITCODE -ne 0) {
    throw "Failed to install project dependencies."
}

& $pythonPath -c "import playwright, rapidocr_onnxruntime; print('Python environment ready')"
if ($LASTEXITCODE -ne 0) {
    throw "Environment verification failed."
}

Write-Host "Environment: $environmentPath"
Write-Host "Run: $pythonPath app.py --check"
