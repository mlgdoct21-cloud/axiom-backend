from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.orm import declarative_base
import os
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Phase 2: PostgreSQL with asyncpg (async driver)
# Falls back to SQLite in development if DATABASE_URL not set or PostgreSQL unavailable
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "sqlite+aiosqlite:///axiom.db"
)

# Convert PostgreSQL URL to async asyncpg format
# Railway uses "postgres://" prefix, Heroku uses "postgres://" too
# SQLAlchemy needs "postgresql+asyncpg://"
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = "postgresql+asyncpg://" + DATABASE_URL[len("postgres://"):]
elif DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = "postgresql+asyncpg://" + DATABASE_URL[len("postgresql://"):]

# Engine configuration
connect_args = {}
if "sqlite" in DATABASE_URL:
    connect_args = {"check_same_thread": False}
else:
    # For asyncpg: only valid connect_args (no "timeout" - that's a connection param not connect_arg)
    connect_args = {
        "server_settings": {"application_name": "axiom_bot"}
    }

engine = create_async_engine(
    DATABASE_URL,
    echo=False,
    connect_args=connect_args,
    pool_pre_ping=True,  # Test connection before using
    pool_recycle=3600   # Recycle connections every hour
)

# Her istekte yeni bir oturum (Session) açacak sessionmaker
AsyncSessionLocal = async_sessionmaker(bind=engine, expire_on_commit=False)

# Tüm tabloların türeyeceği Ana Sınıf
Base = declarative_base()

# FastAPI routerlarında veritabanı bağlantısını güvenle alıp kapatmak için dependency fonksiyonu
async def get_db():
    async with AsyncSessionLocal() as session:
        try:
            yield session
        finally:
            await session.close()
