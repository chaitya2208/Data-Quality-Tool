# Getting Started - Data Quality Platform

This guide will get you up and running in under 10 minutes.

## Prerequisites Checklist

- [ ] Python 3.9 or higher installed
- [ ] Docker installed (for PostgreSQL)
- [ ] Snowflake account with credentials (or skip for demo mode)
- [ ] Terminal/Command prompt access

## Installation Steps

### Step 1: Navigate to Project

```bash
cd C:\Users\cshah\Downloads\Data_Quality\backend
```

### Step 2: Quick Setup

**Windows:**
```bash
quickstart.bat
```

**Mac/Linux:**
```bash
chmod +x quickstart.sh
./quickstart.sh
```

This script will:
1. ✅ Create `.env` file from template (edit with your Snowflake credentials)
2. ✅ Create Python virtual environment
3. ✅ Install all dependencies
4. ✅ Start PostgreSQL in Docker
5. ✅ Initialize database tables
6. ✅ Create default rules
7. ✅ Test connections

### Step 3: Configure Snowflake (or Skip for Demo)

Edit `.env` file with your Snowflake credentials:

```env
SNOWFLAKE_ACCOUNT=your_account
SNOWFLAKE_USER=your_user
SNOWFLAKE_PASSWORD=your_password
SNOWFLAKE_WAREHOUSE=your_warehouse
SNOWFLAKE_DATABASE=your_database  # optional
SNOWFLAKE_SCHEMA=your_schema      # optional
SNOWFLAKE_ROLE=your_role          # optional
```

**Don't have Snowflake yet?** No problem! Skip to Step 5 for demo mode.

### Step 4: Test Connection (Optional)

```bash
python test_connection.py
```

This will verify:
- ✅ PostgreSQL connection
- ✅ Snowflake connection
- ✅ Database tables exist
- ✅ Can query Snowflake metadata

### Step 5: Start the API Server

```bash
uvicorn app.main:app --reload
```

You should see:
```
INFO:     Uvicorn running on http://127.0.0.1:8000
INFO:     Application startup complete.
```

### Step 6: Verify It's Working

Open your browser and visit:

**API Documentation (Interactive):**
http://localhost:8000/api/v1/docs

**Health Check:**
http://localhost:8000/health

You should see:
```json
{"status": "healthy"}
```

## First Scan - Two Ways

### Way 1: With Snowflake

**1. Discover your databases:**
```bash
curl http://localhost:8000/api/v1/assets/discover/databases
```

**2. List tables in a schema:**
```bash
curl http://localhost:8000/api/v1/assets/discover/tables/YOUR_DB/YOUR_SCHEMA
```

**3. Trigger a scan:**
```bash
curl -X POST http://localhost:8000/api/v1/scans/table \
  -H "Content-Type: application/json" \
  -d '{
    "database": "YOUR_DATABASE",
    "schema": "YOUR_SCHEMA",
    "table": "YOUR_TABLE"
  }'
```

**4. View findings:**
```bash
curl http://localhost:8000/api/v1/findings
```

### Way 2: Demo Mode (No Snowflake)

**1. Load demo data:**
```bash
python demo_data.py
```

This creates:
- 12 sample tables across 2 databases
- 36 sample columns
- 12 scans
- ~20 findings

**2. View findings:**
```bash
curl http://localhost:8000/api/v1/findings/stats/summary
```

**3. List all assets:**
```bash
curl http://localhost:8000/api/v1/assets
```

## Using the Interactive API Docs

The easiest way to explore the API is through the interactive documentation:

1. Visit: http://localhost:8000/api/v1/docs
2. Click on any endpoint to expand it
3. Click "Try it out"
4. Fill in parameters
5. Click "Execute"
6. See the response

### Try These First:

#### 1. Get Findings Summary
- Endpoint: `GET /api/v1/findings/stats/summary`
- Click "Try it out" → "Execute"
- See breakdown by status and severity

#### 2. List All Findings
- Endpoint: `GET /api/v1/findings`
- Try filters:
  - `severity=high`
  - `status=detected`
  - `limit=10`

#### 3. Update a Finding
- Endpoint: `PATCH /api/v1/findings/{finding_id}`
- Get a finding_id from the list above
- Set status to "validated"
- Add a note

## Understanding the Data Model

### Asset
A Snowflake object: database, schema, table, or column
- `fqn`: Fully qualified name (e.g., "DB.SCHEMA.TABLE")
- `owner`: Who owns it
- `comment`: Documentation
- `metadata`: Raw metadata from Snowflake

### Scan
An execution of rules against an asset
- `status`: pending → running → completed/failed
- `findings_count`: Number of issues found
- `rules_checked`: Number of rules executed

### Finding
A detected data quality issue
- `title`: Summary of the issue
- `severity`: critical/high/medium/low/info
- `status`: detected → validated → assigned → resolved → closed
- `evidence`: What triggered this finding

### Rule
A quality check to perform
- `code`: Unique identifier (e.g., "MISSING_TABLE_COMMENT")
- `category`: documentation, ownership, schema, etc.
- `applies_to`: Which asset types (table, column, etc.)
- `is_active`: Whether to run this rule

## Common Workflows

### Workflow 1: Initial Discovery
```bash
# 1. What databases do we have?
GET /api/v1/assets/discover/databases

# 2. What schemas are in DB?
GET /api/v1/assets/discover/schemas/MY_DB

# 3. What tables are in schema?
GET /api/v1/assets/discover/tables/MY_DB/PUBLIC

# 4. Scan a table
POST /api/v1/scans/table
{
  "database": "MY_DB",
  "schema": "PUBLIC",
  "table": "USERS"
}

# 5. View findings
GET /api/v1/findings
```

### Workflow 2: Review and Triage
```bash
# 1. Get summary stats
GET /api/v1/findings/stats/summary

# 2. List high severity issues
GET /api/v1/findings?severity=high

# 3. Review a specific finding
GET /api/v1/findings/{id}

# 4. Validate it's a real issue
PATCH /api/v1/findings/{id}
{
  "status": "validated"
}

# 5. Assign to someone
PATCH /api/v1/findings/{id}
{
  "status": "assigned",
  "assigned_to": "data-team@company.com"
}
```

### Workflow 3: Resolution
```bash
# 1. Fix the issue in Snowflake (manually for now)

# 2. Mark as resolved
PATCH /api/v1/findings/{id}
{
  "status": "resolved",
  "resolution_notes": "Added table comment with business context"
}

# 3. Re-scan to verify
POST /api/v1/scans/table
{
  "database": "...",
  "schema": "...",
  "table": "..."
}
```

## What The Rules Check

### Current Rules (Phase 0):

**1. MISSING_TABLE_COMMENT** (Medium Severity)
- Checks if tables have documentation
- Why: Undocumented tables are hard to understand and use
- Fix: Add COMMENT ON TABLE

**2. MISSING_TABLE_OWNER** (High Severity)
- Checks if tables have an assigned owner
- Why: Need accountability and point of contact
- Fix: Update ownership metadata

**3. MISSING_COLUMN_COMMENT** (Low Severity)
- Checks if columns have documentation
- Why: Column purpose should be clear
- Fix: Add COMMENT ON COLUMN

### Coming Soon:
- Naming convention violations
- Missing primary keys
- Unused tables (no queries in 90 days)
- PII without masking
- And more...

## Troubleshooting

### Problem: Can't connect to PostgreSQL

**Solution:**
```bash
# Check if Docker is running
docker ps

# If not, start it
docker-compose up -d

# Wait a few seconds for PostgreSQL to start
sleep 5

# Try again
python test_connection.py
```

### Problem: Can't connect to Snowflake

**Check:**
1. Is `.env` file correct?
2. Can you connect manually? (SnowSQL, web UI)
3. Does your user have permissions?
4. Is your IP whitelisted?

**Workaround:**
Use demo mode: `python demo_data.py`

### Problem: Import errors

**Solution:**
```bash
# Make sure virtual environment is activated
# You should see (venv) in your prompt

# Windows
venv\Scripts\activate

# Mac/Linux
source venv/bin/activate

# Verify Python location
which python  # Should point to venv/bin/python
```

### Problem: Port 8000 already in use

**Solution:**
```bash
# Use a different port
uvicorn app.main:app --reload --port 8001

# Or find and kill the process using port 8000
# Windows:
netstat -ano | findstr :8000
taskkill /PID <PID> /F

# Mac/Linux:
lsof -i :8000
kill -9 <PID>
```

## Next Steps

Now that you have the backend running:

### Option 1: Explore via API Docs
Visit http://localhost:8000/api/v1/docs and try different endpoints

### Option 2: Build Frontend
Create a React dashboard to visualize findings

### Option 3: Add More Rules
Extend the rule engine with custom checks

### Option 4: Set Up Scheduled Scans
Configure automatic scanning on a schedule

See [STATUS.md](STATUS.md) for the full roadmap.

## Quick Reference

### Important URLs
- API: http://localhost:8000
- Docs: http://localhost:8000/api/v1/docs
- Health: http://localhost:8000/health

### Key Commands
```bash
# Start API
uvicorn app.main:app --reload

# Test connections
python test_connection.py

# Load demo data
python demo_data.py

# Clear demo data
python demo_data.py clear

# Database setup
python setup_db.py
```

### Key Files
- `.env` - Configuration
- `api_examples.http` - API examples
- `SETUP.md` - Detailed setup guide
- `STATUS.md` - Current status and roadmap

## Getting Help

1. Check [SETUP.md](SETUP.md) for detailed instructions
2. Check [ARCHITECTURE.md](ARCHITECTURE.md) for technical details
3. Run `python test_connection.py` to diagnose issues
4. Review API docs at http://localhost:8000/api/v1/docs

---

**Ready to build something awesome? Let's go! 🚀**
