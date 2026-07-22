[CmdletBinding()]
param(
    [ValidateRange(1024, 65535)]
    [int]$Port = 2024
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$pythonExecutable = Join-Path $projectRoot ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $pythonExecutable)) {
    throw "Run scripts/bootstrap.ps1 before starting the Agent Server."
}

$portClient = [System.Net.Sockets.TcpClient]::new()
$portOccupied = $false
try {
    $connectTask = $portClient.ConnectAsync("127.0.0.1", $Port)
    if ($connectTask.Wait(500) -and $portClient.Connected) {
        $portOccupied = $true
    }
} catch {
    $portOccupied = $false
} finally {
    $portClient.Dispose()
}
if ($portOccupied) {
    throw "Port $Port is already occupied. Stop the existing Agent Server first."
}

$env:PYTHONUTF8 = "1"

& $pythonExecutable -c "from langgraph_cli.cli import cli; cli()" dev `
    --no-browser `
    --no-reload `
    --host 127.0.0.1 `
    --port $Port
if ($LASTEXITCODE -ne 0) {
    throw "The local Agent Server stopped with an error."
}
