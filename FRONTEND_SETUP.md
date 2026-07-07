# Frontend Setup Guide

## Prerequisites

- Node.js 18+ installed
- Backend API running on http://localhost:8000

## Quick Setup

### 1. Navigate to frontend directory

```bash
cd frontend
```

### 2. Run setup script

```bash
setup.bat
```

This will install all dependencies (~2-3 minutes).

### 3. Start development server

```bash
npm run dev
```

The dashboard will open at: **http://localhost:3000**

## Manual Setup

If the script doesn't work:

```bash
# Install dependencies
npm install

# Start dev server
npm run dev
```

## What You'll See

### Dashboard (/)
- **Statistics Cards**: Total findings, assets scanned, scans completed
- **Charts**: Severity distribution (pie chart), status distribution (bar chart)
- **Recent Findings**: Latest 5 quality issues

### Scanner (/scanner)
- **Step 1**: Select Snowflake database (dropdown)
- **Step 2**: Select schema (dropdown)
- **Step 3**: Select table (dropdown)
- **Scan Button**: Triggers quality scan
- **Results**: Shows findings count and rules checked

### Findings (/findings)
- **Filters**: By severity (Critical/High/Medium/Low) and status
- **List View**: All findings with badges
- **Actions**: Validate findings
- **Details**: Full description, evidence, timestamps

### Assets (/assets)
- **Table View**: All scanned tables
- **Columns**: Table name, owner, row count, last scanned
- **Sorting**: Click column headers

## Troubleshooting

### Port 3000 already in use

```bash
# Kill process on port 3000 (Windows)
netstat -ano | findstr :3000
taskkill /PID <PID> /F

# Or use a different port
npm run dev -- --port 3001
```

### Backend API not reachable

Make sure backend is running:
```bash
cd ..\backend
.\venv\Scripts\python.exe -m uvicorn app.main:app --reload
```

Check API is responding:
```bash
curl http://localhost:8000/health
```

### NPM install fails

```bash
# Clear cache
npm cache clean --force

# Delete node_modules and try again
rmdir /s /q node_modules
npm install
```

### Module not found errors

```bash
# Reinstall dependencies
npm install
```

## Development

### File Structure

```
src/
├── api/
│   └── client.ts          # API client
├── pages/
│   ├── Dashboard.tsx      # Main dashboard
│   ├── Scanner.tsx        # Table scanner
│   ├── Findings.tsx       # Findings list
│   └── Assets.tsx         # Assets table
├── App.tsx                # Main app + routing
└── main.tsx              # Entry point
```

### Making Changes

1. Edit files in `src/`
2. Changes auto-reload in browser
3. Check console for errors

### Building for Production

```bash
npm run build
```

Builds to `dist/` folder.

## Features

### Visual Design
- ✅ Clean, modern interface
- ✅ Tailwind CSS styling
- ✅ Responsive layout
- ✅ Professional color scheme

### Dashboard
- ✅ Statistics cards with icons
- ✅ Interactive charts (Recharts)
- ✅ Recent findings feed
- ✅ Real-time data updates

### Scanner
- ✅ 3-step wizard
- ✅ Dynamic dropdowns
- ✅ Loading states
- ✅ Success/error messages

### Findings
- ✅ Severity badges (color-coded)
- ✅ Status badges
- ✅ Filters (severity + status)
- ✅ One-click validation

### Assets
- ✅ Sortable table
- ✅ Formatted numbers
- ✅ Hover states
- ✅ Empty states

## Next Steps

Once the frontend is running:

1. **Open Dashboard** - http://localhost:3000
2. **Go to Scanner** - Click "Scanner" in sidebar
3. **Scan a Table** - Select database → schema → table → Scan
4. **View Results** - Go to "Findings" to see detected issues
5. **Check Assets** - Go to "Assets" to see scanned tables

## Screenshots

*Coming soon - add screenshots after first run*

## Need Help?

1. Check backend is running: http://localhost:8000
2. Check browser console for errors (F12)
3. Review backend logs for API errors
4. Check CORS settings in backend
