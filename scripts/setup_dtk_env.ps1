param(
    [Parameter(Mandatory = $false)]
    [string]$BackendRoot = (Join-Path (Split-Path (Split-Path $PSScriptRoot -Parent) -Parent) "Douyin_TikTok_Download_API")
)

$ErrorActionPreference = "Stop"
$backendPath = [System.IO.Path]::GetFullPath($BackendRoot)
$envPath = Join-Path $backendPath ".env"

if (-not (Test-Path -LiteralPath (Join-Path $backendPath "docker\compose.yml"))) {
    throw "DTK backend checkout not found at: $backendPath"
}
if (Test-Path -LiteralPath $envPath) {
    throw "Refusing to overwrite existing DTK environment file: $envPath"
}

function New-RandomHex([int]$ByteCount) {
    $bytes = [byte[]]::new($ByteCount)
    [System.Security.Cryptography.RandomNumberGenerator]::Fill($bytes)
    return [Convert]::ToHexString($bytes).ToLowerInvariant()
}

function New-RandomBase64([int]$ByteCount) {
    $bytes = [byte[]]::new($ByteCount)
    [System.Security.Cryptography.RandomNumberGenerator]::Fill($bytes)
    return [Convert]::ToBase64String($bytes)
}

$postgresPassword = New-RandomHex 24
$redisPassword = New-RandomHex 24
$secretKey = New-RandomBase64 48
$downloaderToken = New-RandomHex 24

$lines = @(
    "DTK_SECRET_KEY=$secretKey"
    "POSTGRES_PASSWORD=$postgresPassword"
    "REDIS_PASSWORD=$redisPassword"
    "DTK_DATABASE_URL=postgresql+asyncpg://dtk:${postgresPassword}@postgres:5432/dtk"
    "DTK_REDIS_URL=redis://:${redisPassword}@redis:6379/0"
    "DTK_LOG_LEVEL=info"
    "DTK_LOG_JSON=true"
    "DTK_BIND_HOST=127.0.0.1"
    "DTK_BIND_PORT=8000"
    "DTK_IMAGE=evil0ctal/douyin_tiktok_download_api"
    "DTK_IMAGE_TAG=5.0.3"
    "DTK_DOWNLOADER_IMAGE=evil0ctal/douyin_tiktok_download_api-downloader"
    "DTK_DOWNLOADER_URL=http://downloader:9100"
    "DTK_DOWNLOADER_TOKEN=$downloaderToken"
    "DTK_DOWNLOADER_WORKERS=2"
    "DTK_DOWNLOADER_ITEM_WORKERS=2"
    "DTK_REDIS_MAXMEMORY=256mb"
)

[System.IO.File]::WriteAllLines($envPath, $lines, [System.Text.UTF8Encoding]::new($false))
Write-Output "Created DTK environment file at $envPath"
Write-Output "Back up this file together with the Docker volumes; it contains the encryption key."
