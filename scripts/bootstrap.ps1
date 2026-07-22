[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$uvVersion = "0.11.28"
$uvArchiveSha256 = "0a23463216d09c6a72ff80ef5dc5a795f07dc1575cb84d24596c2f124a441b7b"
$uvExecutableSha256 = "533fe4044bc50b05ac89f4d07925597fdb5285369724e8986ecab356818f09ee"
$uvUrl = "https://github.com/astral-sh/uv/releases/download/$uvVersion/uv-x86_64-pc-windows-msvc.zip"
$toolsRoot = Join-Path $projectRoot ".tools"
$uvRoot = Join-Path $toolsRoot "uv"
$uvArchive = Join-Path $toolsRoot "uv-$uvVersion.zip"
$uvExecutable = Join-Path $uvRoot "uv.exe"
$pythonRoot = Join-Path $toolsRoot "python"

New-Item -ItemType Directory -Force -Path $toolsRoot | Out-Null

if (-not (Test-Path -LiteralPath $uvExecutable)) {
    if (-not (Test-Path -LiteralPath $uvArchive)) {
        Invoke-WebRequest -Uri $uvUrl -OutFile $uvArchive
    }

    $actualHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $uvArchive).Hash.ToLowerInvariant()
    if ($actualHash -ne $uvArchiveSha256) {
        throw "The downloaded uv archive failed SHA-256 verification."
    }

    New-Item -ItemType Directory -Force -Path $uvRoot | Out-Null
    Expand-Archive -LiteralPath $uvArchive -DestinationPath $uvRoot -Force
}

$actualExecutableHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $uvExecutable).Hash.ToLowerInvariant()
if ($actualExecutableHash -ne $uvExecutableSha256) {
    throw "The uv executable failed SHA-256 verification."
}

$reportedVersion = (& $uvExecutable --version).Trim()
$escapedVersion = [Regex]::Escape($uvVersion)
if ($reportedVersion -notmatch "^uv $escapedVersion(?:\s|$)") {
    throw "Expected uv $uvVersion but found $reportedVersion."
}

$env:UV_CACHE_DIR = Join-Path $projectRoot ".cache\uv"
$env:UV_PYTHON_INSTALL_DIR = $pythonRoot
$env:UV_PYTHON_BIN_DIR = Join-Path $pythonRoot "bin"

& $uvExecutable python install 3.12.13 --install-dir $pythonRoot --no-bin
if ($LASTEXITCODE -ne 0) {
    throw "uv could not install the pinned Python runtime."
}

& $uvExecutable sync --frozen
if ($LASTEXITCODE -ne 0) {
    throw "uv could not reproduce the locked environment."
}

& $uvExecutable run python --version
& $uvExecutable lock --check
