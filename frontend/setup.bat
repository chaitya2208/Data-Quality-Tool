@echo off
echo ==========================================
echo Data Quality Platform - Frontend Setup
echo ==========================================
echo.

echo Installing dependencies...
call npm install

if %errorlevel% neq 0 (
    echo.
    echo ERROR: Failed to install dependencies
    echo.
    echo Troubleshooting:
    echo 1. Make sure Node.js is installed (node --version)
    echo 2. Make sure npm is installed (npm --version)
    echo 3. Try: npm cache clean --force
    exit /b 1
)

echo.
echo ==========================================
echo Setup complete!
echo ==========================================
echo.
echo To start the development server:
echo    npm run dev
echo.
echo The app will be available at:
echo    http://localhost:3000
echo.
echo Make sure the backend API is running:
echo    cd ..\backend
echo    .\venv\Scripts\python.exe -m uvicorn app.main:app --reload
echo.
