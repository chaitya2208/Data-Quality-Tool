@echo off
echo Installing dependencies for Windows (Corporate Network)...
echo.

REM Activate venv if not already active
call venv\Scripts\activate.bat

echo Upgrading pip...
python -m pip install --upgrade pip --trusted-host pypi.org --trusted-host files.pythonhosted.org

echo.
echo Installing packages with SSL workaround...
echo.

REM Set trusted hosts to bypass SSL issues
set PIP_ARGS=--trusted-host pypi.org --trusted-host files.pythonhosted.org --trusted-host pypi.python.org

echo [1/5] Installing core web framework...
pip install %PIP_ARGS% fastapi==0.109.0
pip install %PIP_ARGS% uvicorn[standard]==0.27.0

echo.
echo [2/5] Installing validation (using older compatible version)...
pip install %PIP_ARGS% pydantic==2.4.2
pip install %PIP_ARGS% pydantic-settings==2.0.3

echo.
echo [3/3] Installing Snowflake connector...
pip install %PIP_ARGS% snowflake-connector-python==3.6.0

echo.
echo Installing utilities...
pip install %PIP_ARGS% python-dotenv==1.0.0
pip install %PIP_ARGS% python-multipart==0.0.6
pip install %PIP_ARGS% python-dateutil==2.8.2

echo.
echo Installing testing packages...
pip install %PIP_ARGS% pytest==7.4.4
pip install %PIP_ARGS% pytest-asyncio==0.23.3
pip install %PIP_ARGS% httpx==0.26.0

echo.
echo Installing security packages...
pip install %PIP_ARGS% "python-jose[cryptography]==3.3.0"
pip install %PIP_ARGS% "passlib[bcrypt]==1.7.4"

echo.
echo ==========================================
echo Installation complete!
echo ==========================================
echo.
echo Next steps:
echo 1. python setup_db.py
echo 2. python test_sso.py
echo.
