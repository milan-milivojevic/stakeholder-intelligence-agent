[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$uvExecutable = Join-Path $projectRoot ".tools\uv\uv.exe"

if (-not (Test-Path -LiteralPath $uvExecutable)) {
    throw "Run scripts/bootstrap.ps1 before quality checks."
}

$env:UV_CACHE_DIR = Join-Path $projectRoot ".cache\uv"
$env:UV_PYTHON_INSTALL_DIR = Join-Path $projectRoot ".tools\python"
$env:UV_PYTHON_BIN_DIR = Join-Path $projectRoot ".tools\python\bin"
$qualityTemp = Join-Path $projectRoot ".cache\quality-temp"
$pytestCache = Join-Path $projectRoot ".cache\pytest-cache"
$pytestTemp = Join-Path $qualityTemp ("pytest-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Force -Path $qualityTemp, $pytestCache | Out-Null
$env:TEMP = $qualityTemp
$env:TMP = $qualityTemp

& $uvExecutable lock --check
if ($LASTEXITCODE -ne 0) { throw "The dependency lock is stale." }

& $uvExecutable run ruff format --check src tests scripts
if ($LASTEXITCODE -ne 0) { throw "Ruff formatting failed." }

& $uvExecutable run ruff check src tests scripts
if ($LASTEXITCODE -ne 0) { throw "Ruff linting failed." }

& $uvExecutable run mypy src tests scripts
if ($LASTEXITCODE -ne 0) { throw "Mypy failed." }

& $uvExecutable run pytest `
    -o "cache_dir=$pytestCache" `
    --basetemp $pytestTemp `
    --cov=stakeholder_intelligence_agent `
    --cov-report=term-missing `
    -m "not live and not e2e and not slow"
if ($LASTEXITCODE -ne 0) { throw "Offline tests failed." }

& $uvExecutable run bandit -c pyproject.toml -r src
if ($LASTEXITCODE -ne 0) { throw "Bandit failed." }
