# Windows: kjør i prosjektmappen:  powershell -ExecutionPolicy Bypass -File .\setup.ps1
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    Write-Host "Python mangler. Installer fra https://www.python.org/downloads/ og huk av Add to PATH."
    exit 1
}

if (-not (Test-Path .venv)) {
    python -m venv .venv
}

& .\.venv\Scripts\python.exe -m pip install --upgrade pip
& .\.venv\Scripts\python.exe -m pip install -r requirements.txt

if (-not (Test-Path .env)) {
    Copy-Item .env.example .env
    Write-Host ""
    Write-Host "Opprettet .env — fyll inn XAI_API_KEY, POLYMARKET_PRIVATE_KEY og POLYMARKET_FUNDER."
    notepad .env
}

Write-Host ""
Write-Host "Ferdig. Aktiver venv og kjør:"
Write-Host "  .\.venv\Scripts\Activate.ps1"
Write-Host "  python -m agent.bootstrap_creds"
Write-Host "  python main.py once"
