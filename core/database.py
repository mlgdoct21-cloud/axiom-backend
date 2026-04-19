from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.orm import declarative_base
import os
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Phase 2: PostgreSQL with asyncpg (async driver)
# Falls back to SQLite in development if DATABASE_URL not set
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "sqlite+aiosqlite:///axiom.db"
)

# Convert sync PostgreSQL URL to async if needed
if "postgresql://" in DATABASE_URL:
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://")

# Engine configuration
connect_args = {}
if "sqlite" in DATABASE_URL:
    connect_args = {"check_same_thread": False}

engine = create_async_engine(
    DATABASE_URL,
    echo=False,
    connect_args=connect_args
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
