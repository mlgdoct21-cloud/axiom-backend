"""Trade Dossier model — sembol başına AL/TUT/SAT sentezi cache.

Lean v3 Faz 3 — Mehmet'in kişisel trade aracı. Bir sembol için TA + Temel +
Haber + Makro sentezini tek bir Gemini 2.5 Flash çağrısıyla üretir. 30dk DB
cache (aynı sembol tekrar açılınca taze değilse yeniden üretmek yerine
döndür). `payload` JSONB — şema services/dossier_service.py'de.
"""
from __future__ import annotations

from datetime import datetime
from sqlalchemy import Column, String, DateTime, BigInteger, Text
from sqlalchemy.dialects.postgresql import JSONB

from core.database import Base


class TradeDossier(Base):
    __tablename__ = "trade_dossiers"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    symbol = Column(String(16), nullable=False, index=True)
    symbol_type = Column(String(8), nullable=False)  # "BIST" | "CRYPTO" | "US"
    payload = Column(JSONB, nullable=False)
    model_used = Column(String(32), nullable=False, default="gemini-2.5-flash")
    error = Column(Text)  # üretim başarısızsa hata mesajı (payload yine de yazılır boş şablonla)
    created_at = Column(DateTime(timezone=True), nullable=False, default=datetime.utcnow, index=True)
