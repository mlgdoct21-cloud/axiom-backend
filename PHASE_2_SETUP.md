# Phase 2 — Setup & Infrastructure Guide

**Status:** 🚀 Foundation prepared (April 13, 2026)  
**Duration:** 30-60 minutes  
**Last Update:** 2026-04-13 09:30 UTC

---

## ✅ What's Been Prepared

### Code Structure
```
✅ alembic/                     — Database migrations system
   ├── env.py                   — Alembic configuration
   ├── alembic.ini              — Alembic settings
   └── versions/
       └── 001_initial_schema.py — Initial database schema

✅ routers/v1/                  — API v1 structure
   └── __init__.py              — Router registration (stub)

✅ schemas/                     — Pydantic request/response schemas
   ├── __init__.py
   ├── user_schema.py           — User CRUD schemas
   ├── news_schema.py           — News schemas
   └── error_schema.py          — Error response schema

✅ services/auth.py             — JWT tokens + password hashing
   └── AuthService class        — Token management

✅ docker-compose.dev.yml       — Local dev environment (PostgreSQL + Redis)

✅ requirements.txt (updated)   — All Phase 2 dependencies
```

### Key Features
- ✅ PostgreSQL ready (migration scripts prepared)
- ✅ JWT authentication framework
- ✅ Pydantic validation schemas
- ✅ Structured API routers
- ✅ Docker environment (local dev)
- ✅ Alembic migrations system

---

## 📦 Next Step: Install & Test

### 1. Install New Dependencies

```bash
cd /Users/mehmetgulec/Documents/AXIOM/AXİOM/backend
source venv/bin/activate
pip install -r requirements.txt
```

**Expected:** ~2-3 minutes, 30+ packages installed

### 2. Start PostgreSQL (Docker)

```bash
docker-compose -f docker-compose.dev.yml up -d
```

**Expected:** PostgreSQL ready on localhost:5432

Verify:
```bash
docker-compose -f docker-compose.dev.yml ps
```

### 3. Test Database Connection

```bash
# Inside venv
python -c "from core.database import DATABASE_URL; print(f'DB: {DATABASE_URL}')"
```

### 4. Run Initial Migration

```bash
# When ready to apply schema
alembic upgrade head
```

---

## 🏗️ Architecture Overview

### Current State
```
Phase 1: ✅ PRODUCTION
├── SQLite database
├── Telegram bot (7/24)
├── RSS crawler
├── Gemini AI analysis
└── 1000+ users capable

Phase 2: 🚀 FOUNDATION READY
├── PostgreSQL schema (prepared)
├── JWT authentication (prepared)
├── API structure (prepared)
├── Schemas/Validation (prepared)
└── Docker environment (prepared)
```

### Data Migration Strategy
```
Week 1 Timeline:
  MON-TUE: Backup SQLite → Migrate to PostgreSQL
  TUE-WED: Verify integrity → Run tests
  WED-THU: User auth implementation
  THU-FRI: API endpoints (register, login, profile)
```

---

## 📝 Files Summary

| File | Purpose | Status |
|------|---------|--------|
| `alembic/env.py` | Migration executor | ✅ Ready |
| `alembic/versions/001_*.py` | Schema definition | ✅ Ready |
| `schemas/*.py` | Request validation | ✅ Ready |
| `services/auth.py` | JWT + password | ✅ Ready |
| `routers/v1/__init__.py` | API routing | ⏳ Stub (ready to extend) |
| `requirements.txt` | Dependencies | ✅ Updated |
| `docker-compose.dev.yml` | Local environment | ✅ Ready |

---

## 🔧 Configuration Needed

### Update `.env` for PostgreSQL

```bash
# Add to .env:
DATABASE_URL=postgresql://axiom:axiom@localhost:5432/axiom
SECRET_KEY=your-super-secret-key-change-in-production
ALGORITHM=HS256
ACCESS_TOKEN_EXPIRE_MINUTES=15
```

### Environment Setup

```bash
# Create .env.local for development
cat >> .env << 'EOF'
DATABASE_URL=postgresql://axiom:axiom@localhost:5432/axiom
SECRET_KEY=axiom-dev-secret-key-change-in-production-12345
ALGORITHM=HS256
ACCESS_TOKEN_EXPIRE_MINUTES=15
REFRESH_TOKEN_EXPIRE_DAYS=7
EOF
```

---

## ✨ Ready for Week 1 Implementation

This foundation is ready for:

### Week 1 Tasks
- [ ] PostgreSQL migration (SQLite → PostgreSQL)
- [ ] User migration (safe data transfer)
- [ ] Auth service testing
- [ ] First API endpoints

### Week 2 Tasks
- [ ] API structure expansion
- [ ] Monitoring setup
- [ ] Health checks
- [ ] API documentation

---

## 🚀 To Continue

**When ready to start Week 1 implementation:**

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Start PostgreSQL
docker-compose -f docker-compose.dev.yml up -d

# 3. Test connection
alembic current

# 4. Ready for implementation
echo "✅ Foundation ready, Week 1 can begin!"
```

---

## 📞 Support

If issues with:
- **PostgreSQL:** Check `docker-compose logs postgres`
- **Dependencies:** Run `pip check`
- **Migrations:** Run `alembic current`
- **Schema:** Review `alembic/versions/001_*.py`

---

**Status:** 🟢 READY FOR WEEK 1 IMPLEMENTATION

Start Monday with Phase 2.0 sprint!
