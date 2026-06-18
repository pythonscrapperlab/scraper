#!/bin/bash
# Quick Start Script for Aevorex Scraper

set -e

echo "=== Aevorex Scraper — Quick Start ==="
echo ""

# Check Python version
echo "✓ Checking Python version..."
python --version
echo ""

# Create venv
echo "✓ Creating virtual environment..."
if [ ! -d "venv" ]; then
    python -m venv venv
    echo "  Created venv/"
else
    echo "  venv/ already exists"
fi
echo ""

# Activate venv (on Windows)
echo "✓ Activating virtual environment..."
if [ -f "venv/Scripts/activate" ]; then
    source venv/Scripts/activate
elif [ -f "venv/bin/activate" ]; then
    source venv/bin/activate
fi
echo "  venv activated"
echo ""

# Install dependencies
echo "✓ Installing dependencies..."
pip install -r requirements.txt
echo "  Dependencies installed"
echo ""

# Copy .env if missing
echo "✓ Setting up configuration..."
if [ ! -f ".env" ]; then
    cp .env.example .env
    echo "  Created .env (update with your settings)"
else
    echo "  .env exists"
fi
echo ""

# Start Docker containers
echo "✓ Starting Docker containers..."
docker-compose up -d
echo "  Waiting 10 seconds for Postgres to be ready..."
sleep 10
echo "  Docker containers started"
echo ""

# Run migrations
echo "✓ Running Alembic migrations..."
alembic upgrade head
echo "  Database initialized"
echo ""

echo "=== Setup Complete! ==="
echo ""
echo "Next steps:"
echo "  1. Implement scrapers (scrapers/zillow.py, etc.)"
echo "  2. Run: python main.py scrape --source zillow --state FL"
echo "  3. Check database: psql -h localhost -U aevorex -d aevorex"
echo ""
echo "Useful commands:"
echo "  docker-compose logs postgres"
echo "  docker-compose stop"
echo "  docker-compose down"
echo ""
