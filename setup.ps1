# Quick Start Script for Aevorex Scraper (Windows)
# Run as: powershell -ExecutionPolicy Bypass -File setup.ps1

Write-Host "=== Aevorex Scraper — Quick Start (Windows) ===" -ForegroundColor Cyan
Write-Host ""

# Check Python version
Write-Host "✓ Checking Python version..." -ForegroundColor Green
python --version
Write-Host ""

# Create venv
Write-Host "✓ Creating virtual environment..." -ForegroundColor Green
if (-not (Test-Path "venv")) {
    python -m venv venv
    Write-Host "  Created venv\" -ForegroundColor Yellow
} else {
    Write-Host "  venv\ already exists" -ForegroundColor Yellow
}
Write-Host ""

# Activate venv
Write-Host "✓ Activating virtual environment..." -ForegroundColor Green
& ".\venv\Scripts\Activate.ps1"
Write-Host "  venv activated" -ForegroundColor Yellow
Write-Host ""

# Install dependencies
Write-Host "✓ Installing dependencies..." -ForegroundColor Green
pip install -r requirements.txt
Write-Host "  Dependencies installed" -ForegroundColor Yellow
Write-Host ""

# Copy .env if missing
Write-Host "✓ Setting up configuration..." -ForegroundColor Green
if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    Write-Host "  Created .env (update with your settings)" -ForegroundColor Yellow
} else {
    Write-Host "  .env exists" -ForegroundColor Yellow
}
Write-Host ""

# Start Docker containers
Write-Host "✓ Starting Docker containers..." -ForegroundColor Green
docker-compose up -d
Write-Host "  Waiting 10 seconds for Postgres to be ready..." -ForegroundColor Yellow
Start-Sleep -Seconds 10
Write-Host "  Docker containers started" -ForegroundColor Yellow
Write-Host ""

# Run migrations
Write-Host "✓ Running Alembic migrations..." -ForegroundColor Green
alembic upgrade head
Write-Host "  Database initialized" -ForegroundColor Yellow
Write-Host ""

Write-Host "=== Setup Complete! ===" -ForegroundColor Cyan
Write-Host ""
Write-Host "Next steps:" -ForegroundColor Green
Write-Host "  1. Implement scrapers (scrapers/zillow.py, etc.)"
Write-Host "  2. Run: python main.py scrape --source zillow --state FL"
Write-Host "  3. Check database: psql -h localhost -U aevorex -d aevorex"
Write-Host ""
Write-Host "Useful commands:" -ForegroundColor Green
Write-Host "  docker-compose logs postgres"
Write-Host "  docker-compose stop"
Write-Host "  docker-compose down"
Write-Host ""
