@echo off
echo ==========================================
echo Setting up Python environment...
echo ==========================================

REM Check if virtual environment exists
if not exist venv (
    echo Creating virtual environment...
    python -m venv venv
    if %errorlevel% neq 0 (
        echo ERROR: Failed to create virtual environment
        echo Make sure Python 3.9+ is installed
        exit /b 1
    )
    echo Virtual environment created
)

echo.
echo Activating virtual environment...
call venv\Scripts\activate.bat

echo.
echo Installing dependencies...
pip install --upgrade pip
pip install -r requirements.txt

if %errorlevel% neq 0 (
    echo ERROR: Failed to install dependencies
    exit /b 1
)

echo.
echo ==========================================
echo Setup complete!
echo ==========================================
echo.
echo Next steps:
echo 1. Make sure Docker Desktop is running
echo 2. Run: docker-compose up -d
echo 3. Run: python setup_db.py
echo 4. Run: python test_connection.py
echo.
