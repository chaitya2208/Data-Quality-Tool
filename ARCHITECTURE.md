# Phase 0 Architecture

## What We Built

```
┌─────────────────────────────────────────────────────────────┐
│                      FastAPI Backend                        │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐    │
│  │   Assets     │  │    Scans     │  │   Findings   │    │
│  │   API        │  │    API       │  │   API        │    │
│  └──────┬───────┘  └──────┬───────┘  └──────┬───────┘    │
│         │                 │                  │             │
│         └─────────────────┼──────────────────┘             │
│                           │                                │
│  ┌────────────────────────▼──────────────────────────┐    │
│  │          Scan Service                             │    │
│  │  - Orchestrates scanning workflow                 │    │
│  │  - Creates/updates assets                         │    │
│  │  - Executes rules                                 │    │
│  │  - Generates findings                             │    │
│  └────────────┬────────────────────┬─────────────────┘    │
│               │                    │                       │
│  ┌────────────▼──────────┐  ┌──────▼────────────────┐    │
│  │  Snowflake Connector  │  │    Rule Engine        │    │
│  │  - Connection mgmt    │  │  - Execute rules      │    │
│  │  - Metadata queries   │  │  - Check violations   │    │
│  │  - List assets        │  │  - Generate findings  │    │
│  └────────────┬──────────┘  └───────────────────────┘    │
│               │                                            │
└───────────────┼────────────────────────────────────────────┘
                │
     ┌──────────▼──────────┐
     │   PostgreSQL DB     │
     │                     │
     │  - assets           │
     │  - scans            │
     │  - findings         │
     │  - rules            │
     └─────────────────────┘
                │
     ┌──────────▼──────────┐
     │    Snowflake        │
     │  (Metadata Source)  │
     └─────────────────────┘
```

## Domain Model

```
┌─────────────┐
│   Asset     │  (Database, Schema, Table, Column)
├─────────────┤
│ id          │
│ fqn         │  Fully qualified name
│ asset_type  │  database/schema/table/column
│ owner       │
│ comment     │
│ metadata    │  JSON metadata from Snowflake
└──────┬──────┘
       │ 1:N
       │
┌──────▼──────┐
│    Scan     │  (Execution of rules against asset)
├─────────────┤
│ id          │
│ asset_id    │
│ scan_type   │  metadata/schema/data_profile
│ status      │  pending/running/completed/failed
│ findings    │
└──────┬──────┘
       │ 1:N
       │
┌──────▼──────┐       ┌─────────────┐
│  Finding    │  N:1  │    Rule     │
├─────────────┤───────┤─────────────┤
│ id          │       │ id          │
│ asset_id    │       │ code        │
│ scan_id     │       │ name        │
│ rule_id     │       │ category    │
│ title       │       │ severity    │
│ description │       │ applies_to  │
│ status      │       │ is_active   │
│ severity    │       └─────────────┘
│ evidence    │
└─────────────┘
```

## Request Flow: Scan a Table

```
1. User Request
   POST /api/v1/scans/table
   { "database": "DB", "schema": "SCHEMA", "table": "TABLE" }
   
2. Scan Service
   ├─ Connect to Snowflake
   ├─ Fetch table metadata (owner, comment, row_count, size)
   ├─ Fetch column definitions
   ├─ Create/update Asset records
   └─ Create Scan record (status: RUNNING)

3. Rule Engine
   ├─ Get active rules for asset_type="table"
   ├─ Execute each rule (check violations)
   │  ├─ MISSING_TABLE_COMMENT → Check if comment exists
   │  ├─ MISSING_TABLE_OWNER → Check if owner exists
   │  └─ ...
   ├─ Get active rules for asset_type="column" 
   └─ Execute rules for each column

4. Finding Generation
   ├─ For each violation, create Finding record
   │  ├─ title: Human-readable summary
   │  ├─ description: Detailed explanation
   │  ├─ severity: From rule definition
   │  ├─ evidence: What triggered the finding
   │  └─ status: DETECTED
   └─ Link findings to scan

5. Scan Completion
   ├─ Update Scan (status: COMPLETED)
   ├─ Set findings_count
   ├─ Set rules_checked
   └─ Return scan details

6. Response
   {
     "id": "scan-uuid",
     "status": "completed",
     "findings_count": 5,
     "rules_checked": 3,
     ...
   }
```

## Default Rules (Phase 0)

| Code | Name | Category | Severity | Applies To |
|------|------|----------|----------|------------|
| MISSING_TABLE_COMMENT | Missing Table Comment | DOCUMENTATION | MEDIUM | table |
| MISSING_TABLE_OWNER | Missing Table Owner | OWNERSHIP | HIGH | table |
| MISSING_COLUMN_COMMENT | Missing Column Comment | DOCUMENTATION | LOW | column |

## API Endpoints Summary

### Discovery
- `GET /assets/discover/databases` - List Snowflake databases
- `GET /assets/discover/schemas/{db}` - List schemas
- `GET /assets/discover/tables/{db}/{schema}` - List tables

### Scanning
- `POST /scans/table` - Trigger table scan
- `GET /scans` - List all scans
- `GET /scans/{id}` - Get scan details

### Findings
- `GET /findings` - List findings (with filters)
- `GET /findings/{id}` - Get finding details
- `PATCH /findings/{id}` - Update finding status
- `GET /findings/stats/summary` - Get summary stats

### Rules
- `GET /rules` - List all rules
- `POST /rules/initialize` - Initialize default rules

## What's Working

✅ Connect to Snowflake  
✅ Discover databases, schemas, tables  
✅ Fetch table metadata  
✅ Create Asset records  
✅ Execute deterministic rules  
✅ Generate Findings  
✅ Track Scan history  
✅ CRUD operations for all entities  
✅ Filter and query findings  

## What's Next (Phase 1)

- React frontend
- Background job processing
- More sophisticated rules
- Scheduled scans
- Email notifications
- AI recommendations (Phase 2)
