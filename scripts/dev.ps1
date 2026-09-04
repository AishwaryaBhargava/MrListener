<#
    Starts the MrListener backend and frontend together (same as `npm run dev`).
    Usage:  .\scripts\dev.ps1
    Stop both with Ctrl+C.
#>
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root 'backend\.venv\Scripts\python.exe'

if (-not (Test-Path $python)) {
    throw "Backend venv not found at $python. Run 'npm run setup' first."
}
if (-not (Test-Path (Join-Path $root 'frontend\node_modules'))) {
    throw "Frontend dependencies missing. Run 'npm run setup' first."
}

Write-Host 'MrListener' -ForegroundColor Cyan
Write-Host '  backend   http://localhost:8000  (docs at /docs)'
Write-Host '  frontend  http://localhost:5173'
Write-Host ''

$backend = Start-Process -FilePath $python `
    -ArgumentList '-m', 'uvicorn', 'app.main:app', '--reload', '--port', '8000' `
    -WorkingDirectory (Join-Path $root 'backend') -NoNewWindow -PassThru

$frontend = Start-Process -FilePath 'npm.cmd' `
    -ArgumentList 'run', 'dev' `
    -WorkingDirectory (Join-Path $root 'frontend') -NoNewWindow -PassThru

try {
    while (-not $backend.HasExited -and -not $frontend.HasExited) {
        Start-Sleep -Milliseconds 500
    }
}
finally {
    Write-Host "`nShutting down..." -ForegroundColor Yellow
    foreach ($proc in @($backend, $frontend)) {
        if ($proc -and -not $proc.HasExited) {
            taskkill /PID $proc.Id /T /F 2>&1 | Out-Null
        }
    }
}
