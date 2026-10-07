$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot

Push-Location $ProjectRoot
try {
    python -m pytest -q

    Push-Location (Join-Path $ProjectRoot "frontend")
    try {
        npm.cmd run lint
        npm.cmd run build
    }
    finally {
        Pop-Location
    }

    docker compose config --quiet
    Write-Host "Preflight passed: backend tests, frontend lint/build, and Compose validation."
}
finally {
    Pop-Location
}
