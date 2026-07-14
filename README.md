# Enterprise Data Quality Platform

An AI-powered data quality platform for Snowflake with finding-centric architecture.

## 🎯 What Is This?


A comprehensive platform to monitor, assess, and improve data quality in Snowflake warehouses. It combines:
- **Automated scanning** of metadata, schema, and data quality
- **Rule-based detection** of quality issues
- **Finding-centric workflow** for tracking and resolving issues
- **AI recommendations** for fixes (coming in Phase 2)
- **Human-in-the-loop** approval before changes

## ✅ Current Status: Phase 0 Complete

**Phase 0 (Foundation)** is fully implemented and working:
- ✅ App storage schema in Snowflake (assets/scans/findings/rules/etc.)
- ✅ Snowflake connector for metadata queries
- ✅ Rule engine with 3 deterministic rules
- ✅ FastAPI backend with REST API
- ✅ Scan orchestration and finding generation
- ✅ Complete CRUD operations

See [STATUS.md](STATUS.md) for detailed progress and [ARCHITECTURE.md](ARCHITECTURE.md) for technical details.

## 🚀 Quick Start

### Complete Stack (Backend + Frontend)

```bash
# Setup frontend (one-time)
cd frontend
setup.bat
cd ..

# Start both services
start_all.bat
```

This opens two windows:
- **Backend API**: http://localhost:8000
- **Frontend Dashboard**: http://localhost:3000

### Backend Only

```bash
cd backend
.\quickstart.bat
```

Visit API docs: http://localhost:8000/docs

### Frontend Only

```bash
cd frontend
npm install
npm run dev
```

Visit dashboard: http://localhost:3000

## 📁 Project Structure

```
Data_Quality/
├── README.md              # This file
├── SETUP.md              # Detailed setup guide
├── ARCHITECTURE.md       # Architecture documentation
├── STATUS.md            # Current status and roadmap
│
└── backend/
    ├── requirements.txt     # Python dependencies
    ├── snowflake/           # DDL: app schema + tables + default rule seed
    ├── setup_db.py         # Runs snowflake/*.sql against the app schema
    ├── test_connection.py  # Connection tests
    ├── api_examples.http   # API examples
    │
    └── app/
        ├── main.py            # FastAPI app
        ├── core/             # Configuration + shared enums
        ├── schemas/          # Pydantic schemas
        ├── services/         # Business logic + storage.py (raw-SQL data layer)
        └── api/              # REST endpoints
```

## 🔧 What You Can Do Now

### 1. Discover Snowflake Assets
Browse databases, schemas, and tables in your Snowflake warehouse.

### 2. Scan Tables
Trigger metadata scans that check for:
- Missing table comments/documentation
- Missing table owners
- Missing column comments

### 3. View Findings
See all detected data quality issues with:
- Severity levels (critical, high, medium, low)
- Status tracking (detected, validated, assigned, resolved)
- Filtering and search
- Summary statistics

### 4. Manage Rules
View, create, and configure quality rules.

## 📊 API Endpoints

Full interactive documentation at: http://localhost:8000/api/v1/docs

Key endpoints:
- `POST /api/v1/scans/table` - Trigger a scan
- `GET /api/v1/findings` - List findings
- `GET /api/v1/findings/stats/summary` - Get statistics
- `GET /api/v1/assets` - List scanned assets
- `GET /api/v1/rules` - List quality rules

See [api_examples.http](backend/api_examples.http) for usage examples.

## 🗺️ Roadmap

### ✅ Phase 0: Foundation (COMPLETE)
- Core domain model
- Snowflake connector
- Basic rule engine
- REST API

### 🔄 Phase 0.5: Quick Wins (NEXT)
- React frontend dashboard
- Background job processing
- More rules
- Email notifications

### 📋 Phase 1: Core Platform
- Scheduled scans
- User authentication
- Recommendation workflow
- Audit trail
- Dashboard with charts

### 🤖 Phase 2: AI Integration
- LangGraph agents
- AI recommendations
- Verification agent
- RAG over governance docs
- Historical learning

### 🚀 Phase 3: Advanced Features
- Knowledge graph
- Predictive insights
- Multi-agent collaboration
- Shift-left PR validation

## 🧪 Testing

```bash
# Test connections
python test_connection.py

# Create demo data
python demo_data.py

# Clear demo data
python demo_data.py clear

# Run API
uvicorn app.main:app --reload
```

## 📝 Documentation

- **[SETUP.md](SETUP.md)** - Step-by-step setup instructions
- **[ARCHITECTURE.md](ARCHITECTURE.md)** - Technical architecture and design
- **[STATUS.md](STATUS.md)** - Current status and next steps
- **[api_examples.http](backend/api_examples.http)** - API usage examples

## 🛠️ Tech Stack

- **Backend**: Python 3.9+, FastAPI
- **Storage & Data Warehouse**: Snowflake (app tables and source data both live here — see `backend/snowflake/`)
- **API Docs**: OpenAPI/Swagger

## 🤝 Contributing

This is a step-by-step project. Each phase is implemented incrementally to make debugging easier.

## 📄 License

Private project.

## 🎯 Next Steps

Choose your path:
1. **Frontend** - Build React dashboard (recommended)
2. **More Rules** - Add naming conventions, PII detection
3. **Background Jobs** - Async scan processing
4. **AI Integration** - Start with simple recommendations

See [STATUS.md](STATUS.md) for detailed next steps and options.
