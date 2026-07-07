@echo off
echo ==========================================
echo Quick Start with SQLite (No Docker)
echo ==========================================
echo.

REM Activate virtual environment
echo Activating virtual environment...
call venv\Scripts\activate.bat

REM Temporarily switch to SQLite
echo Switching to SQLite database...
powershell -Command "(gc .env) -replace 'DATABASE_URL=postgresql.*', 'DATABASE_URL=sqlite:///./data_quality.db' | Out-File -encoding ASCII .env"

echo.
echo Initializing database...
python setup_db.py

if %errorlevel% neq 0 (
    echo ERROR: Database initialization failed
    exit /b 1
)

echo.
echo Testing Snowflake SSO connection...
echo Your browser will open for authentication...
echo.
python test_sso.py

echo.
echo ==========================================
echo To switch back to PostgreSQL later:
echo 1. Start Docker Desktop
echo 2. Run: docker compose up -d
echo 3. Update .env to use postgresql://...
echo 4. Run: python setup_db.py
echo ==========================================
