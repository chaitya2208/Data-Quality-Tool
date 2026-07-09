@echo off
REM Quick start script for Data Quality Platform backend (Windows)

echo ==========================================
echo Data Quality Platform - Quick Start
echo ==========================================
echo.

REM Check if .env exists
if not exist .env (
    echo WARNING: .env file not found!
    echo Creating .env from .env.example...
    copy .env.example .env
    echo Created .env file
    echo.
    echo IMPORTANT: Edit .env and add your Snowflake credentials!
    echo Then run this script again.
    exit /b 1
)

REM Check if virtual environment exists
if not exist venv (
    echo Creating virtual environment...
    python -m venv venv
    echo Virtual environment created
)

REM Activate virtual environment
echo Activating virtual environment...
call venv\Scripts\activate.bat
echo Virtual environment activated

REM Install dependencies
echo.
echo Installing dependencies...
pip install -q --upgrade pip
pip install -q -r requirements.txt
echo Dependencies installed

REM Setup database (creates the app schema + tables in Snowflake)
echo.
echo Setting up database...
python setup_db.py
if %errorlevel% neq 0 (
    echo Failed to setup database
    exit /b 1
)
echo Database initialized

REM Test connections
echo.
echo Testing connections...
python test_connection.py

if %errorlevel% equ 0 (
    echo.
    echo ==========================================
    echo Setup complete!
    echo ==========================================
    echo.
    echo To start the API server:
    echo   uvicorn app.main:app --reload
    echo.
    echo API will be available at:
    echo   - http://localhost:8000
    echo   - http://localhost:8000/api/v1/docs
    echo.
) else (
    echo.
    echo WARNING: Connection tests failed
    echo Please check the errors above and fix them
    exit /b 1
)
