from sqlalchemy import Column, Integer, String, Text, DateTime, Boolean
from sqlalchemy.sql import func
from core.database import Base

class NewsItem(Base):
    __tablename__ = "news_items"

    id = Column(Integer, primary_key=True, index=True)
    source = Column(String, index=True) # Örn: "Bloomberg", "SEC"
    original_title = Column(String, nullable=False)
    original_link = Column(String, unique=True, nullable=False)

    # Gemini'den dönen 3 maddelik özet metni
    ai_summary = Column(Text, nullable=True)
    # AI'ın atadığı etiketler (Örn: "BTC,Kripto,Kriz")
    ai_tags = Column(String, nullable=True)

    # 3-Tier Data Architecture Columns
    telegram_hook = Column(Text, nullable=True)
    dashboard_summary = Column(Text, nullable=True)
    axiom_analysis = Column(Text, nullable=True)

    # ── Two-Stage Pipeline Columns ──────────────────────────────────────────────
    # FMP sembolü (ör: "AAPL", "BTCUSD") — FMP context çekmek için kullanılır.
    symbol = Column(String(32), nullable=True, index=True)
    # AI analizi yapıldı mı? (Fast fetch loop = False, batch analyze loop = True)
    analyzed = Column(Boolean, nullable=False, default=False, server_default="false", index=True)
    # Telegram'a broadcast edildi mi? (NULL = henüz gönderilmedi)
    broadcast_at = Column(DateTime(timezone=True), nullable=True)
    # Acil mi? (breaking, crash, earnings vb.) — smart broadcast routing için.
    is_urgent = Column(Boolean, nullable=False, default=False, server_default="false")
    # Haberin ham gövdesi (AI prompt'una girdi) — başlık yetersiz olduğunda
    # Gemini'nin jenerik analiz dönmemesi için kullanılır.
    body = Column(Text, nullable=True)

    # Haberin çekildiği ve veritabanına işlendiği tarih
    created_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)
