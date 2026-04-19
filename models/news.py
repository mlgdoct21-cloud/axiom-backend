from sqlalchemy import Column, Integer, String, Text, DateTime
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
    
    # Haberin çekildiği ve veritabanına işlendiği tarih
    created_at = Column(DateTime(timezone=True), server_default=func.now())
