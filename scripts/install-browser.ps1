[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
$browserRoot = Join-Path $projectRoot ".cache\playwright-browsers"

if (-not (Test-Path -LiteralPath $python)) {
    throw "Run scripts/bootstrap.ps1 before installing the browser runtime."
}

New-Item -ItemType Directory -Force -Path $browserRoot | Out-Null
$env:PLAYWRIGHT_BROWSERS_PATH = $browserRoot
& $python -m playwright install chromium
if ($LASTEXITCODE -ne 0) {
    throw "The pinned project-local Chromium runtime could not be installed."
}

& $python -m playwright --version
