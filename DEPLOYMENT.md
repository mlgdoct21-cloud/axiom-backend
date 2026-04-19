# 🚀 AXIOM Production Deployment Guide

## Quick Deployment (After FAZA 1.2 - Supabase Ready)

### Option A: FastAPI + Bot Background (Recommended)
**Best for:** API-first deployment + background bot  
**Run:** `uvicorn main:app --reload`  
**Pros:** HTTP API endpoints available + Telegram bot in background  
**Cons:** Bot runs as background task (may not auto-restart on failure)

### Option B: Autonomous Bot Mode
**Best for:** Pure bot deployment without API  
**Run:** `python bot_runner.py`  
**Pros:** Self-healing, autonomous, full control  
**Cons:** No HTTP API endpoints

---

## Deployment to Railway (FastAPI Mode)

### Prerequisites
- Railway account: https://railway.app
- Supabase project: Fully initialized (DNS resolved)
- GitHub repository with backend code

### Steps

1. **Initialize Git (DONE)**
   ```bash
   cd /Users/mehmetgulec/Documents/AXIOM/AXİOM/backend
   git init
   git add -A
   git commit -m "Initial: AXIOM backend"
   ```

2. **Create GitHub Repository**
   - Go to https://github.com/new
   - Repository name: `axiom-backend`
   - Push code: 
     ```bash
     git remote add origin https://github.com/YOUR_USERNAME/axiom-backend.git
     git push -u origin main
     ```

3. **Connect to Railway**
   - Go to https://railway.app/dashboard
   - Click "New Project" → "Deploy from GitHub"
   - Select `axiom-backend` repository
   - Railway auto-detects Dockerfile and builds

4. **Configure Environment Variables**
   - In Railway dashboard, go to Variables tab
   - Add:
     ```
     TELEGRAM_BOT_TOKEN=8702083982:AAGqr2z7n3v_r6122dMoWD2TnjDP2f868Ac
     GEMINI_API_KEY=AIzaSyCo0GaMEMAQxGCrGTvnmnN7fj2IMmCyowc
     DATABASE_URL=postgresql://postgres:aWbb6QtEipqJ7o56@db.enpaxcwxjurpymboahm.supabase.co:5432/postgres
     ```

5. **Run Migrations**
   - SSH into Railway container:
     ```bash
     railway run bash
     ```
   - Inside container:
     ```bash
     alembic upgrade head
     exit
     ```

6. **Deploy**
   - Click "Deploy" in Railway dashboard
   - Wait for build to complete (~3-5 min)
   - Check logs for errors

### Verify Deployment
```bash
# Your backend will be at:
https://axiom-backend-prod-xxxx.railway.app

# Test health endpoint:
curl https://axiom-backend-prod-xxxx.railway.app/health

# View logs:
# Use Railway dashboard → Logs tab
```

---

## Deployment to Vercel (Next.js Frontend)

### Prerequisites
- Vercel account: https://vercel.com
- Next.js code ready
- GitHub repository

### Steps

1. **Connect Frontend Repo to Vercel**
   - Go to https://vercel.com/dashboard
   - Click "Add New..." → "Project"
   - Import `axiom-dashboard` from GitHub
   - Vercel auto-configures Next.js

2. **Configure Environment Variables**
   - In Vercel dashboard, go to Settings → Environment Variables
   - Add:
     ```
     NEXT_PUBLIC_FINNHUB_API_KEY=d7gisohr01qmqj45heq0d7gisohr01qmqj45heqg
     NEXT_PUBLIC_API_BASE=https://axiom-backend-prod-xxxx.railway.app
     GEMINI_API_KEY=AIzaSyCo0GaMEMAQxGCrGTvnmnN7fj2IMmCyowc
     ```

3. **Deploy**
   - Vercel auto-deploys on git push to main
   - Or click "Deploy" in dashboard

4. **View Live**
   - Your frontend will be at:
     ```
     https://axiom-dashboard.vercel.app
     ```

---

## Production Architecture

```
┌─────────────────────────────────────┐
│    VERCEL (Frontend)                │
│  axiom-dashboard.vercel.app         │
│  - Next.js 16                       │
│  - React 19                         │
│  - API calls to Backend             │
└──────────────┬──────────────────────┘
               │
               ↓ (HTTPS API)
┌─────────────────────────────────────┐
│    RAILWAY (Backend)                │
│  axiom-backend.railway.app          │
│  - FastAPI                          │
│  - 6-Agent Pipeline                 │
│  - Telegram Bot (background)        │
│  - RSS Crawler                      │
└──────────────┬──────────────────────┘
               │
               ↓ (SQL)
┌─────────────────────────────────────┐
│    SUPABASE (Database)              │
│  db.enpaxcwxjurpymboahm.supabase.co │
│  - PostgreSQL (500MB free)          │
│  - users table                      │
│  - news_items table                 │
│  - sources table                    │
└─────────────────────────────────────┘
```

---

## Cost Breakdown

| Service | Cost | Purpose |
|---------|------|---------|
| Vercel | Free | Frontend hosting |
| Railway | Free ($5 credit) | Backend + bot |
| Supabase | Free | PostgreSQL database |
| Finnhub | Free | Stock data APIs |
| Gemini | ~$2/mo | AI analysis |
| **TOTAL** | **~$2/mo** | Production system |

---

## Monitoring & Logs

### Vercel Logs
- Dashboard → Project → Deployments → Logs
- Real-time function logs
- Error tracking

### Railway Logs
- Dashboard → Project → Logs
- All container output
- Deployment history

### Supabase Monitoring
- Dashboard → SQL Editor
- Monitor queries and connections
- Check usage (free tier: 500MB storage, 1M requests/month)

---

## Health Checks

### Backend Health
```bash
curl https://axiom-backend-prod-xxxx.railway.app/health
```

### Database Connection
```python
# Test in Railway SSH:
python -c "
import psycopg2
conn = psycopg2.connect('postgresql://...')
print('✅ Database connected')
"
```

### Telegram Bot
- Check `/getMe` endpoint:
  ```bash
  curl https://api.telegram.org/bot{TOKEN}/getMe
  ```
- Send test message to bot and verify response

---

## Troubleshooting

### "Database connection refused"
- Verify Supabase is fully initialized
- Check DATABASE_URL in Environment Variables
- Run: `alembic upgrade head` in Railway SSH

### "Telegram bot not responding"
- Verify TELEGRAM_BOT_TOKEN in Environment Variables
- Check bot logs in Railway dashboard
- Test: `/start` command in Telegram

### "Frontend API 404 errors"
- Check NEXT_PUBLIC_API_BASE in Vercel env vars
- Verify backend is running (check Railway logs)
- Ensure CORS is enabled in FastAPI

---

## Rollback & Recovery

### Revert to Previous Deploy
- Vercel: Dashboard → Deployments → Click previous version → "Redeploy"
- Railway: Dashboard → Deployments → Select older deployment

### Database Rollback
- **WARNING:** Only if needed
  ```bash
  cd backend
  alembic downgrade base
  alembic upgrade head
  ```

---

## Next Steps (After Production Live)

1. **Monitor**: Set up error tracking (Sentry, LogRocket)
2. **Scale**: Add caching (Redis) for price data
3. **Enhance**: WebSocket for real-time updates
4. **Security**: Set up rate limiting, API key rotation
5. **Alerts**: Create monitoring alerts for failures

---

**Last Updated:** April 19, 2026  
**Status:** Ready to deploy once Supabase is initialized  
**Estimated Deploy Time:** 15-20 minutes
