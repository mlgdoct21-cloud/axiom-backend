# API Quick Start Guide

## Starting the System

### 1. Ensure PostgreSQL & Redis are Running

```bash
cd /Users/mehmetgulec/Documents/AXIOM/AXİOM/backend
docker-compose -f docker-compose.dev.yml ps
```

Expected output:
```
NAME                 STATUS
axiom-postgres-dev   Up (healthy)
axiom-redis-dev      Up (healthy)
```

If not running:
```bash
docker-compose -f docker-compose.dev.yml up -d
```

### 2. Activate Virtual Environment

```bash
cd /Users/mehmetgulec/Documents/AXIOM/AXİOM/backend
source venv/bin/activate
```

### 3. Start FastAPI Server

```bash
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

Or without reload (production):
```bash
uvicorn main:app --host 0.0.0.0 --port 8000
```

Expected output:
```
INFO:     Uvicorn running on http://0.0.0.0:8000
INFO:     Application startup complete
```

### 4. Access API Documentation

- **Swagger UI:** http://localhost:8000/api/docs
- **ReDoc:** http://localhost:8000/api/redoc
- **OpenAPI JSON:** http://localhost:8000/api/openapi.json

---

## API Workflow Example

### Step 1: Register a User

```bash
curl -X POST http://localhost:8000/api/v1/auth/register \
  -H "Content-Type: application/json" \
  -d '{
    "telegram_id": "user_12345",
    "username": "John Doe"
  }'
```

Response:
```json
{
  "id": 1,
  "telegram_id": "user_12345",
  "username": "John Doe",
  "is_active": true,
  "tags": "",
  "report_mode": "digest",
  "report_hours": "08:00",
  "custom_follows": "",
  "created_at": "2026-04-13T10:00:00",
  "updated_at": null
}
```

### Step 2: Login

```bash
curl -X POST http://localhost:8000/api/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{"telegram_id": "user_12345"}'
```

Response:
```json
{
  "access_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
  "refresh_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
  "token_type": "bearer",
  "user": {
    "id": 1,
    "telegram_id": "user_12345",
    "username": "John Doe"
  }
}
```

Save the `access_token` for authenticated requests.

### Step 3: Get User Profile

```bash
curl http://localhost:8000/api/v1/users/me \
  -H "Authorization: Bearer <access_token>"
```

### Step 4: Update Settings

```bash
curl -X PUT http://localhost:8000/api/v1/users/me/settings \
  -H "Authorization: Bearer <access_token>" \
  -H "Content-Type: application/json" \
  -d '{
    "tags": "BTC,ETH,AAPL",
    "report_mode": "digest",
    "report_hours": "09:00,18:00",
    "custom_follows": "Tesla,Microsoft"
  }'
```

### Step 5: Get News

```bash
curl "http://localhost:8000/api/v1/news?limit=10&skip=0" \
  -H "Authorization: Bearer <access_token>"
```

### Step 6: Search News

```bash
curl "http://localhost:8000/api/v1/news/search?q=bitcoin&limit=10" \
  -H "Authorization: Bearer <access_token>"
```

### Step 7: Filter News by Source

```bash
curl "http://localhost:8000/api/v1/news/source/Bloomberg?limit=10" \
  -H "Authorization: Bearer <access_token>"
```

---

## Common Tasks

### Get OpenAPI Documentation

All available endpoints are documented in OpenAPI format. Visit:
- http://localhost:8000/api/docs (interactive Swagger UI)
- http://localhost:8000/api/redoc (ReDoc documentation)

### Check API Status

```bash
curl http://localhost:8000/api/v1/status
```

### Health Check

```bash
curl http://localhost:8000/health
```

### Verify Database Connection

```bash
python -c "from core.database import engine; import asyncio; asyncio.run(engine.connect())"
```

### View Recent Logs

```bash
docker-compose -f docker-compose.dev.yml logs postgres -f
```

---

## Environment Variables

Located in `.env`:

```bash
# Database
DATABASE_URL=postgresql://axiom:axiom@localhost:5432/axiom

# Authentication
SECRET_KEY=axiom-dev-secret-key-change-in-production-12345
ALGORITHM=HS256
ACCESS_TOKEN_EXPIRE_MINUTES=15
REFRESH_TOKEN_EXPIRE_DAYS=7

# Telegram & AI (from Phase 1)
GEMINI_API_KEY=<your-key>
TELEGRAM_BOT_TOKEN=<your-token>
```

### Security Note
⚠️ **IMPORTANT**: Change `SECRET_KEY` before deploying to production!

---

## Testing Endpoints

Use Python's `httpx` library for async testing:

```python
import asyncio
from httpx import AsyncClient
from main import app

async def test():
    async with AsyncClient(app=app, base_url="http://testserver") as client:
        # Register
        resp = await client.post("/api/v1/auth/register", json={
            "telegram_id": "test_user",
            "username": "Test"
        })
        print(f"Register: {resp.status_code}")
        
        # Login
        resp = await client.post("/api/v1/auth/login", json={
            "telegram_id": "test_user"
        })
        token = resp.json()["access_token"]
        print(f"Login: {resp.status_code}")
        
        # Get profile
        resp = await client.get("/api/v1/users/me", 
            headers={"Authorization": f"Bearer {token}"})
        print(f"Profile: {resp.status_code}")

asyncio.run(test())
```

---

## Troubleshooting

### PostgreSQL Connection Error
```
Error: could not connect to server: Connection refused
```
**Solution:** Start PostgreSQL container
```bash
docker-compose -f docker-compose.dev.yml up -d postgres
```

### Invalid Token Error
```
{"detail": "Invalid or expired token"}
```
**Solution:** Token expired (15 minutes). Get a new one:
```bash
curl -X POST http://localhost:8000/api/v1/auth/refresh \
  -H "Content-Type: application/json" \
  -d '{"refresh_token": "<refresh_token>"}'
```

### Database Migration Issues
```
Error: alembic.command: Can't find migration script
```
**Solution:** Check migrations are applied
```bash
alembic current
alembic upgrade head
```

### Port Already in Use
```
OSError: [Errno 48] Address already in use
```
**Solution:** Change port
```bash
uvicorn main:app --port 8001
```

---

## Development Commands

### Run Tests
```bash
pytest --cov=. --cov-report=html
```

### Format Code
```bash
black . --line-length 100
isort . --profile black
```

### Check Code Quality
```bash
flake8 . --max-line-length=100
mypy . --strict
```

### Database Migrations
```bash
# Create new migration
alembic revision --autogenerate -m "Add new column"

# Apply migrations
alembic upgrade head

# Rollback
alembic downgrade -1
```

---

## API Endpoint Summary

| Method | Endpoint | Auth | Description |
|--------|----------|------|-------------|
| POST | `/auth/register` | ❌ | Register new user |
| POST | `/auth/login` | ❌ | Login & get tokens |
| POST | `/auth/refresh` | ❌ | Refresh access token |
| GET | `/users/me` | ✅ | Get profile |
| PUT | `/users/me` | ✅ | Update profile |
| GET | `/users/me/settings` | ✅ | Get settings |
| PUT | `/users/me/settings` | ✅ | Update settings |
| PUT | `/users/me/tags` | ✅ | Update tags |
| DELETE | `/users/me` | ✅ | Deactivate account |
| GET | `/users/{user_id}` | ✅ | Get user by ID |
| GET | `/users` | ✅ | List all users |
| GET | `/news` | ✅ | Get all news |
| GET | `/news/latest` | ✅ | Get latest news |
| GET | `/news/search` | ✅ | Search news |
| GET | `/news/source/{source}` | ✅ | Filter by source |
| GET | `/news/tag/{tag}` | ✅ | Filter by tag |
| POST | `/news/filter` | ✅ | Advanced filtering |
| GET | `/news/{news_id}` | ✅ | Get single item |

---

## Next Steps

1. **Phase 2.1 (Weeks 3-4):** Build Next.js dashboard
2. **Phase 2.2 (Weeks 5-6):** Integrate technical analysis
3. **Phase 2.3 (Weeks 7-8):** Production hardening & deployment

---

**Happy Coding! 🚀**
