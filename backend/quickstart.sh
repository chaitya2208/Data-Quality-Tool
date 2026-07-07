#!/bin/bash

# Quick start script for Data Quality Platform backend
# This script sets up everything needed to run the backend

set -e  # Exit on error

echo "=========================================="
echo "Data Quality Platform - Quick Start"
echo "=========================================="
echo ""

# Check if .env exists
if [ ! -f .env ]; then
    echo "⚠️  .env file not found!"
    echo "Creating .env from .env.example..."
    cp .env.example .env
    echo "✓ Created .env file"
    echo ""
    echo "⚠️  IMPORTANT: Edit .env and add your Snowflake credentials!"
    echo "Then run this script again."
    exit 1
fi

# Check if virtual environment exists
if [ ! -d "venv" ]; then
    echo "Creating virtual environment..."
    python -m venv venv
    echo "✓ Virtual environment created"
fi

# Activate virtual environment
echo "Activating virtual environment..."
if [[ "$OSTYPE" == "msys" ]] || [[ "$OSTYPE" == "win32" ]]; then
    source venv/Scripts/activate
else
    source venv/bin/activate
fi
echo "✓ Virtual environment activated"

# Install dependencies
echo ""
echo "Installing dependencies..."
pip install -q --upgrade pip
pip install -q -r requirements.txt
echo "✓ Dependencies installed"

# Check if PostgreSQL is running
echo ""
echo "Checking PostgreSQL..."
if docker ps | grep -q dq_postgres; then
    echo "✓ PostgreSQL is running"
else
    echo "Starting PostgreSQL with Docker..."
    docker-compose up -d
    echo "Waiting for PostgreSQL to be ready..."
    sleep 5
    echo "✓ PostgreSQL started"
fi

# Setup database
echo ""
echo "Setting up database..."
python setup_db.py
echo "✓ Database initialized"

# Test connections
echo ""
echo "Testing connections..."
python test_connection.py

if [ $? -eq 0 ]; then
    echo ""
    echo "=========================================="
    echo "✓ Setup complete!"
    echo "=========================================="
    echo ""
    echo "To start the API server:"
    echo "  uvicorn app.main:app --reload"
    echo ""
    echo "API will be available at:"
    echo "  - http://localhost:8000"
    echo "  - http://localhost:8000/api/v1/docs"
    echo ""
else
    echo ""
    echo "⚠️  Connection tests failed"
    echo "Please check the errors above and fix them"
    exit 1
fi
