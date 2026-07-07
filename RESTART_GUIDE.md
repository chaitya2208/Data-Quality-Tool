# Restart Guide - After Connection Pooling Update

## What Changed?

✅ **Backend**: Connection pooling added - SSO happens only ONCE on first use  
✅ **Frontend**: Connects to Snowflake on app startup  
✅ **Frontend**: Caches databases/schemas/tables for 5 minutes  

## How to Restart

### Option 1: Restart Both (Recommended)

**1. Stop current servers:**
- Press `CTRL+C` in both backend and frontend windows

**2. Restart:**
```bash
cd C:\Users\cshah\Downloads\Data_Quality
start_all.bat
```

### Option 2: Restart Individually

**Backend:**
```bash
cd backend
.\venv\Scripts\python.exe -m uvicorn app.main:app --reload
```

**Frontend:**
```bash
cd frontend
npm run dev
```

## What You'll Experience Now

### First Time (After Restart)

1. **Open Dashboard**: http://localhost:3000
2. **See Loading Screen**: "Connecting to Snowflake..."
3. **Browser Opens**: SSO login (only once!)
4. **Dashboard Loads**: You're connected!

### After That

✅ **No more SSO prompts** - Connection is reused  
✅ **Databases already loaded** - Cached from first fetch  
✅ **Schemas already loaded** - Cached per database  
✅ **Tables already loaded** - Cached per schema  

### Connection Status

Look at the sidebar footer:
```
✓ Snowflake Connected
  CSHAH
```

This shows you're authenticated and connected.

## Timeline

### Before (Annoying):
```
1. Open Dashboard → No SSO needed (just dashboard)
2. Go to Scanner → No SSO needed
3. Select Database → SSO prompt #1 😤
4. Select Schema → SSO prompt #2 😤
5. Select Table → SSO prompt #3 😤
6. Scan → SSO prompt #4 😤
```

### After (Smooth):
```
1. Open Dashboard → SSO prompt (ONE TIME) 🎉
2. Dashboard loads → Shows "Snowflake Connected"
3. Go to Scanner → Databases already loaded ⚡
4. Select Database → Schemas load instantly ⚡
5. Select Schema → Tables load instantly ⚡
6. Scan → Works immediately ⚡
```

## Cache Behavior

**Databases/Schemas/Tables are cached for 5 minutes:**
- First visit: Fetches from Snowflake
- Next 5 minutes: Uses cached data
- After 5 minutes: Auto-refreshes on next visit

**To force refresh:**
- Refresh browser (F5)
- Wait 5 minutes

## Connection Pooling

**Backend keeps one connection alive:**
- First API call: Authenticates with SSO
- Subsequent calls: Reuses connection
- Connection stays alive for the session

## Troubleshooting

### "Still seeing multiple SSO prompts"

Make sure you restarted the backend:
```bash
# Stop old backend (CTRL+C)
# Start new backend
cd backend
.\venv\Scripts\python.exe -m uvicorn app.main:app --reload
```

### "Connection timeout"

The pooled connection may have expired:
- Refresh the page (F5)
- It will reconnect automatically

### "Browser doesn't open on startup"

That's okay! It will open when you first access Scanner or any Snowflake data.

## Benefits

✅ **1 SSO login** instead of 4+  
✅ **Faster navigation** - cached data  
✅ **Better UX** - no interruptions  
✅ **Backend optimization** - connection reuse  

Enjoy the smooth experience! 🚀
