C:\D\Agentic-App-v2\Data-Quality-Tool\backend> cd C:\D\Agentic-App-v2\Data-Quality-Tool\backend && .venv\Scripts\activate && uvicorn   app.main:app --port 8002

# Setup Guide - Data Quality Platform

This guide will walk you through setting up the backend for Phase 0.

## Prerequisites

- Python 3.9+
- Snowflake account with credentials (app storage and source-table scanning
  both live in Snowflake — no separate database to install)

## Step 1: Backend Setup

### 1. Create virtual environment

```bash
cd backend
python -m venv venv

# Windows
venv\Scripts\activate

# Linux/Mac
source venv/bin/activate
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Configure environment variables

Copy the example env file and update it:

```bash
cp .env.example .env
```

Edit `.env` with your actual values:

```env
# Snowflake - Update these with your credentials.
# App storage (assets/scans/findings/rules/etc.) lives in SNOWFLAKE_APP_SCHEMA
# inside SNOWFLAKE_DATABASE — separate from whatever source data gets scanned.
SNOWFLAKE_ACCOUNT=your_account
SNOWFLAKE_USER=your_user
SNOWFLAKE_PASSWORD=your_password
SNOWFLAKE_WAREHOUSE=your_warehouse
SNOWFLAKE_DATABASE=PLAYGROUND_DB
SNOWFLAKE_SCHEMA=your_schema
SNOWFLAKE_ROLE=your_role
SNOWFLAKE_APP_SCHEMA=DQ_APP

# API
API_V1_STR=/api/v1
PROJECT_NAME=Data Quality Platform

# Security (generate with: openssl rand -hex 32)
SECRET_KEY=your_secret_key_here

# Environment
ENVIRONMENT=development
```

### 4. Initialize the database

```bash
python setup_db.py
```

This will:
- Create the DQ_APP schema and all app tables in Snowflake (see `backend/snowflake/*.sql`)
- Seed default rules (MISSING_TABLE_COMMENT, MISSING_TABLE_OWNER, MISSING_COLUMN_COMMENT, plus the 12 dynamic rule codes)

## Step 3: Start the Backend Server

```bash
uvicorn app.main:app --reload
```

The API will be available at:
- **API**: http://localhost:8000
- **API Docs**: http://localhost:8000/api/v1/docs
- **Health Check**: http://localhost:8000/health

## Step 4: Test the API

### 1. Check health

```bash
curl http://localhost:8000/health
```

### 2. Discover Snowflake databases

```bash
curl http://localhost:8000/api/v1/assets/discover/databases
```

### 3. List schemas in a database

```bash
curl http://localhost:8000/api/v1/assets/discover/schemas/YOUR_DATABASE
```

### 4. List tables in a schema

```bash
curl http://localhost:8000/api/v1/assets/discover/tables/YOUR_DATABASE/YOUR_SCHEMA
```

### 5. Trigger a scan

```bash
curl -X POST http://localhost:8000/api/v1/scans/table \
  -H "Content-Type: application/json" \
  -d '{
    "database": "YOUR_DATABASE",
    "schema": "YOUR_SCHEMA",
    "table": "YOUR_TABLE"
  }'
```

### 6. List findings

```bash
curl http://localhost:8000/api/v1/findings
```

### 7. Get findings summary

```bash
curl http://localhost:8000/api/v1/findings/stats/summary
```

## What You Can Do Now

After setup, you can:

1. **Discover Snowflake assets** - Browse databases, schemas, and tables
2. **Scan tables** - Run metadata scans that check for:
   - Missing table comments
   - Missing table owners
   - Missing column comments
3. **View findings** - See all detected data quality issues
4. **Track scans** - Monitor scan execution and history
5. **Manage rules** - View and configure quality rules

## API Endpoints

### Assets
- `GET /api/v1/assets` - List all assets
- `GET /api/v1/assets/{id}` - Get asset details
- `GET /api/v1/assets/discover/databases` - Discover databases
- `GET /api/v1/assets/discover/schemas/{db}` - Discover schemas
- `GET /api/v1/assets/discover/tables/{db}/{schema}` - Discover tables

### Scans
- `POST /api/v1/scans/table` - Trigger a table scan
- `GET /api/v1/scans` - List all scans
- `GET /api/v1/scans/{id}` - Get scan details

### Findings
- `GET /api/v1/findings` - List findings (with filters)
- `GET /api/v1/findings/{id}` - Get finding details
- `PATCH /api/v1/findings/{id}` - Update finding status
- `GET /api/v1/findings/stats/summary` - Get summary statistics

### Rules
- `GET /api/v1/rules` - List all rules
- `GET /api/v1/rules/{id}` - Get rule details
- `POST /api/v1/rules` - Create new rule
- `PATCH /api/v1/rules/{id}` - Update rule
- `POST /api/v1/rules/initialize` - Re-initialize default rules

## Troubleshooting

### Snowflake connection issues

1. Verify your credentials in `.env`
2. Check network connectivity to Snowflake
3. Verify your user has necessary permissions

### Import errors

Make sure you're in the virtual environment:
```bash
which python  # Should point to venv/bin/python
```

## Next Steps

With Phase 0 complete, you now have:
- ✅ Core domain model (Asset, Scan, Finding, Rule)
- ✅ Snowflake connector
- ✅ Basic rule engine with 3 rules
- ✅ FastAPI backend
- ✅ Full CRUD operations

**What to build next:**
1. More sophisticated rules
2. Background job processing for async scans
3. AI-powered recommendations (Phase 2)

See the main README.md for the full roadmap.
