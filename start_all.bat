@echo off
echo ==========================================
echo Data Quality Platform - Starting All Services
echo ==========================================
echo.

REM Start backend in a new window
echo Starting backend API...
start "Data Quality API" cmd /k "cd backend && .\venv\Scripts\python.exe -m uvicorn app.main:app --reload"

REM Wait a few seconds for backend to start
timeout /t 3 /nobreak >nul

REM Start frontend in a new window
echo Starting frontend...
start "Data Quality UI" cmd /k "cd frontend && npm run dev"

echo.
echo ==========================================
echo Services starting...
echo ==========================================
echo.
echo Backend API: http://localhost:8000
echo Frontend UI: http://localhost:3000
echo.
echo Two new windows will open:
echo 1. Backend API (port 8000)
echo 2. Frontend UI (port 3000)
echo.
echo Press CTRL+C in each window to stop the services
echo.
pause
