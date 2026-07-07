# Data Quality Platform - Frontend

Modern React dashboard for the Data Quality Platform.

## Features

- 📊 **Dashboard** - Visual overview with charts and statistics
- 🔍 **Scanner** - Discover and scan Snowflake tables
- 📋 **Findings** - Browse and manage quality issues
- 💾 **Assets** - View scanned tables and metadata

## Tech Stack

- **React 18** with TypeScript
- **Vite** - Fast build tool
- **TailwindCSS** - Styling
- **TanStack Query** - Data fetching
- **Recharts** - Charts and visualizations
- **React Router** - Navigation
- **Lucide Icons** - Beautiful icons

## Setup

### 1. Install Dependencies

```bash
npm install
```

### 2. Start Development Server

```bash
npm run dev
```

The app will be available at: **http://localhost:3000**

### 3. Build for Production

```bash
npm run build
```

## Configuration

The frontend connects to the backend API at `http://localhost:8000`.

To change this, edit `src/api/client.ts`:

```typescript
const API_BASE_URL = 'http://localhost:8000/api/v1';
```

## Project Structure

```
src/
├── api/
│   └── client.ts          # API client and types
├── pages/
│   ├── Dashboard.tsx      # Dashboard with stats
│   ├── Scanner.tsx        # Table discovery and scanning
│   ├── Findings.tsx       # Findings list and filters
│   └── Assets.tsx         # Assets table
├── App.tsx                # Main app with routing
├── main.tsx              # Entry point
└── index.css             # Global styles
```

## Usage

### Dashboard
- View overall statistics
- See findings distribution by severity
- Track findings by status
- View recent findings

### Scanner
1. Select a database from Snowflake
2. Choose a schema
3. Pick a table to scan
4. Click "Start Scan"
5. View results in Findings tab

### Findings
- Filter by severity (Critical/High/Medium/Low)
- Filter by status (Detected/Validated/Assigned/Resolved)
- Validate findings
- View detailed evidence

### Assets
- View all scanned tables
- See owner, row count
- Track last scan time

## Development

### Adding New Features

1. Create components in `src/components/`
2. Add pages in `src/pages/`
3. Update routing in `App.tsx`
4. Add API calls in `src/api/client.ts`

### Styling

Uses TailwindCSS utility classes. Colors are defined in `tailwind.config.js`.

Primary color palette: Blue (customizable)

### Icons

Using Lucide React. Import icons:

```typescript
import { Database, AlertCircle } from 'lucide-react'
```

Browse icons: https://lucide.dev/icons/
