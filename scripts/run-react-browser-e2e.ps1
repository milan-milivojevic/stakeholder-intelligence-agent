[CmdletBinding()]
param(
    [switch]$Headed,
    [ValidateSet(
        "tests/e2e/test_react_security.py",
        "tests/e2e/test_react_pm.py",
        "tests/e2e/test_react_stakeholder.py",
        "tests/e2e/test_react_acceptance.py",
        "tests/e2e/test_react_upload_matrix.py",
        "tests/e2e/test_react_preflight.py"
    )]
    [string]$TestFile = "tests/e2e/test_react_security.py",
    [ValidateSet(
        "",
        "agent_server_browser_contract_preflight",
        "pm_cookie_reload_logout_csrf_and_exact_origin",
        "stakeholder_history_replay_forgery_revocation_and_logout",
        "pm_complete_parity_with_real_setup_and_controlled_provider_contracts",
        "stakeholder_complete_parity_with_restart_and_permanent_finish",
        "pm_acceptance_scenarios_use_real_backend",
        "six_formats_use_real_scoped_ingestion_routes",
        "shell_authentication_and_workspace_hierarchy",
        "real_agent_server_starts_scoped_insight_without_blocking_override",
        "protected_preview_and_format_aware_analysis",
        "interview_start_turns_and_restoration",
        "recommendation_continue_finish_and_pm_visibility",
        "authorization_and_cross_engagement_denials",
        "retained_browser_evidence_safety"
    )]
    [string]$CaseFilter = ""
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
$browserRoot = Join-Path $projectRoot ".cache\playwright-browsers"
$frontendIndex = Join-Path $projectRoot "frontend\dist\index.html"
$runId = [DateTime]::UtcNow.ToString("yyyyMMddTHHmmssZ") + "-" + [guid]::NewGuid().ToString("N").Substring(0, 8)
$evidenceDir = Join-Path $projectRoot (".cache\browser-e2e\" + $runId)
$tempRoot = Join-Path $projectRoot (".cache\react-browser-e2e-temp\" + $runId)

if (-not (Test-Path -LiteralPath $python)) {
    throw "Run scripts/bootstrap.ps1 before the React browser suite."
}
if (-not (Test-Path -LiteralPath $browserRoot)) {
    throw "Run scripts/install-browser.ps1 before the React browser suite."
}
if (-not (Test-Path -LiteralPath $frontendIndex)) {
    throw "Run the locked frontend production build before the React browser suite."
}

$portClient = [System.Net.Sockets.TcpClient]::new()
$agentPortOccupied = $false
try {
    $connectTask = $portClient.ConnectAsync("127.0.0.1", 2024)
    if ($connectTask.Wait(500) -and $portClient.Connected) {
        $agentPortOccupied = $true
    }
} catch {
    $agentPortOccupied = $false
} finally {
    $portClient.Dispose()
}
if ($agentPortOccupied) {
    throw "Port 2024 is already occupied. Stop the manual runtime or other E2E run first."
}

New-Item -ItemType Directory -Force -Path $evidenceDir, $tempRoot | Out-Null
$env:PLAYWRIGHT_BROWSERS_PATH = $browserRoot
$env:STAKEHOLDER_REACT_E2E_RUN_ID = $runId
$env:STAKEHOLDER_REACT_E2E_EVIDENCE_DIR = $evidenceDir
$env:TEMP = $tempRoot
$env:TMP = $tempRoot
$env:PYTHONUTF8 = "1"

$arguments = @(
    "-m", "pytest",
    $TestFile,
    "-m", "e2e",
    "--browser", "chromium",
    "--output", (Join-Path $evidenceDir "playwright-output"),
    "--junitxml", (Join-Path $evidenceDir "pytest-results.xml"),
    "--tracing", "off",
    "--screenshot", "off",
    "--video", "off",
    "-o", ("cache_dir=" + (Join-Path $tempRoot "pytest-cache")),
    "--basetemp", (Join-Path $tempRoot "pytest-base"),
    "--tb=short",
    "-ra"
)
if ($Headed) {
    $arguments += "--headed"
}
if ($CaseFilter) {
    $arguments += "-k"
    $arguments += $CaseFilter
}

& $python @arguments
$exitCode = $LASTEXITCODE
Write-Output ("React browser E2E results: " + $evidenceDir)
exit $exitCode
