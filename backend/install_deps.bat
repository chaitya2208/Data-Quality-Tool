@echo off
echo Installing dependencies for Windows...
echo.

REM Activate venv if not already active
call venv\Scripts\activate.bat

echo Installing core packages...
pip install --upgrade pip

echo.
echo Installing FastAPI and web framework...
pip install fastapi==0.109.0
pip install "uvicorn[standard]==0.27.0"
pip install pydantic==2.5.3
pip install pydantic-settings==2.1.0

echo.
echo Installing database packages...
pip install sqlalchemy==2.0.25
pip install alembic==1.13.1

echo.
echo Installing PostgreSQL driver (psycopg3 with binary)...
pip install "psycopg[binary]>=3.1.0"

echo.
echo Installing Snowflake connector...
pip install snowflake-connector-python==3.6.0
pip install snowflake-sqlalchemy==1.5.1

echo.
echo Installing utilities...
pip install python-dotenv==1.0.0
pip install python-multipart==0.0.6
pip install python-dateutil==2.8.2

echo.
echo Installing testing packages...
pip install pytest==7.4.4
pip install pytest-asyncio==0.23.3
pip install httpx==0.26.0

echo.
echo Installing security packages...
pip install "python-jose[cryptography]==3.3.0"
pip install "passlib[bcrypt]==1.7.4"

echo.
echo ==========================================
echo Installation complete!
echo ==========================================
echo.
