# Axiom OS — Phase 1 Backend

Financial co-pilot backend that fetches financial news from RSS feeds, runs AI analysis via Google Gemini, and delivers summaries through Telegram.

## Setup

### 1. Prerequisites

- Python 3.10+
- pip or uv

### 2. Environment Setup

```bash
# Create virtual environment
python -m venv venv

# Activate virtual environment
source venv/bin/activate  # macOS/Linux
# or
venv\Scripts\activate  # Windows
```

### 3. Install Dependencies

```bash
pip install -r requirements.txt
```

### 4. Configuration

Copy `.env.example` to `.env` and add your credentials:

```bash
cp .env.example .env
```

Then edit `.env`:

```
GEMINI_API_KEY=your_gemini_api_key_here
TELEGRAM_BOT_TOKEN=your_telegram_bot_token_here
```

**Get API Keys:**
- **Gemini API:** https://aistudio.google.com/app/apikey
- **Telegram Bot Token:** https://t.me/BotFather

## Running

### Option A: Bot + Crawler (Recommended for Production)

Runs bot and crawler together in a single process:

```bash
python bot_runner.py
```

This will:
- Create database tables
- Seed RSS sources (Investing.com, Yahoo Finance, Bloomberg HT)
- Start Telegram bot (long-polling)
- Start crawler (fetches news every 30 minutes)

### Option B: FastAPI Server

Runs HTTP API with bot as background task:

```bash
uvicorn main:app --reload
```

Crawler is **not** started in this mode.

### Option C: Test the Pipeline

Test RSS → Gemini → output without database:

```bash
python test_flow.py
```

## Architecture

### Two Runtime Modes

**`bot_runner.py` (Primary)**
```
start_telegram_bot()  ─┐
                       ├─ asyncio.gather()
run_crawler()         ─┘
```
- Telegram bot: long-polling loop for `/start`, `/haber`, `/tags`, `/takip`
- Crawler: 30-minute cycle of fetch → filter → analyze → broadcast

**`main.py` (FastAPI)**
- HTTP API server
- Telegram bot as background task
- No crawler

### Data Flow

```
RSS Feeds (3 sources)
    ↓
fetch_all_feeds()           [async, parallel]
    ↓
duplicate_filtrele()        [check DB for link]
    ↓
generate_summary()          [Gemini Flash API]
    ↓
NewsItem saved to DB
    ↓
telegram_gonder()           [broadcast to users]
```

### Database

- **SQLite:** `axiom.db` (auto-created in backend directory)
- **Tables:**
  - `users`: telegram subscribers with tag preferences
  - `news_items`: processed articles with AI summaries
  - `sources`: RSS feed sources

## Commands (Telegram)

| Command | Action |
|---------|--------|
| `/start` | Register and get welcome message |
| `/haber` | Fetch latest news analysis on-demand |
| `/tags` | Select topic tags (BTC, Altın, BIST, etc.) |
| `/takip [keyword]` | Add custom keyword to follow |
| `/takipcikar [keyword]` | Remove keyword from follow list |
| `/takiplistem` | View all subscriptions |

## Available Tags

- BTC — Bitcoin
- Altın — Gold
- BIST — Istanbul Stock Exchange
- Dolar — US Dollar
- Faiz — Interest Rates
- Fed — Federal Reserve
- Euro — EUR
- Petrol — Oil
- Kripto — Cryptocurrency
- Hisse — Stocks

## Project Structure

```
backend/
├── main.py                      # FastAPI server
├── bot_runner.py                # Primary entry point (bot + crawler)
├── test_flow.py                 # Pipeline test
├── requirements.txt             # Python dependencies
├── .env                         # Credentials (GITIGNORED)
├── .env.example                 # Template for .env
├── .gitignore                   # Git ignore rules
│
├── core/
│   ├── __init__.py
│   ├── database.py              # SQLAlchemy setup
│   └── logger.py                # Logging configuration
│
├── models/
│   ├── __init__.py
│   ├── user.py                  # User model
│   ├── news.py                  # NewsItem model
│   └── source.py                # Source model
│
├── services/
│   ├── __init__.py
│   ├── rss_service.py           # RSS feed fetching
│   ├── ai_service.py            # Gemini API integration
│   ├── telegram_bot.py          # Bot command handling
│   ├── crawler.py               # Main crawler loop
│   └── validation.py            # Input validation
│
└── logs/
    └── axiom.log                # Application logs
```

## Troubleshooting

### "TELEGRAM_BOT_TOKEN is missing"

Make sure you have a valid `.env` file with `TELEGRAM_BOT_TOKEN` from @BotFather.

### "GEMINI_API_KEY is missing"

Get your free API key at https://aistudio.google.com/app/apikey.

### Database locked error

SQLite doesn't handle concurrent writes well. The crawler uses race condition prevention with unique indexes and IntegrityError handling.

### No news from RSS feeds

Check if the RSS sources are accessible:
- https://www.investing.com/rss/news.rss
- https://feeds.finance.yahoo.com/rss/2.0/headline?s=AAPL
- https://www.bloomberght.com/rss

## Phase 1 Checklist

- [x] FastAPI backend
- [x] SQLite database with 3 tables
- [x] RSS feed crawling (3 sources)
- [x] Gemini Flash AI analysis
- [x] Telegram bot with 6 commands
- [x] User tag filtering
- [x] Custom keyword following
- [x] 30-minute crawler cycle
- [x] Proper error handling and logging
- [x] Input validation
- [x] Cross-platform database path
- [x] Environment configuration

## Next Phase (Phase 2)

- Next.js web dashboard
- User registration UI
- Tag management UI
- News feed display
