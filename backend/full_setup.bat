@echo off
echo ==========================================
echo Data Quality Platform - Full Setup
echo ==========================================
echo.

REM Step 1: Python Environment
echo [Step 1/4] Setting up Python environment...
if not exist venv (
    python -m venv venv
    if %errorlevel% neq 0 (
        echo ERROR: Failed to create virtual environment
        exit /b 1
    )
)

call venv\Scripts\activate.bat
pip install --upgrade pip >nul 2>&1
pip install -r requirements.txt

if %errorlevel% neq 0 (
    echo ERROR: Failed to install dependencies
    exit /b 1
)
echo    Done!
echo.

REM Step 2: PostgreSQL
echo [Step 2/4] Starting PostgreSQL...
docker ps >nul 2>&1
if %errorlevel% neq 0 (
    echo ERROR: Docker is not running
    echo Please start Docker Desktop and run this script again
    exit /b 1
)

docker-compose up -d
echo    Waiting for PostgreSQL to be ready...
timeout /t 5 /nobreak >nul
echo    Done!
echo.

REM Step 3: Database Setup
echo [Step 3/4] Initializing database...
python setup_db.py
if %errorlevel% neq 0 (
    echo ERROR: Failed to initialize database
    exit /b 1
)
echo    Done!
echo.

REM Step 4: Test Snowflake Connection
echo [Step 4/4] Testing Snowflake SSO connection...
echo    Your browser will open for SSO authentication...
echo.
python test_sso.py

if %errorlevel% equ 0 (
    echo.
    echo ==========================================
    echo Setup Complete!
    echo ==========================================
    echo.
    echo To start the API server:
    echo    uvicorn app.main:app --reload
    echo.
    echo Then visit:
    echo    http://localhost:8000/api/v1/docs
    echo.
) else (
    echo.
    echo ==========================================
    echo Setup completed with warnings
    echo ==========================================
    echo.
    echo Database is ready, but Snowflake connection failed.
    echo You can still:
    echo   1. Start API: uvicorn app.main:app --reload
    echo   2. Fix Snowflake config in .env
    echo   3. Test again: python test_sso.py
    echo.
)
