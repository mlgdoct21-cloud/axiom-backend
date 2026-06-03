"""Watchlist + Dossier Diff models — kişisel takip listesi ve dossier değişim feed'i.

İki tablo:
  - watchlist_items: kullanıcı + sembol + kategori (long_term | swing) +
    opsiyonel maliyet/miktar/not. Supervisor bu listeyi tarar; kategoriye göre
    dossier'i periyodik yeniler.
  - dossier_diffs: iki dossier snapshot'ı arasındaki anlamlı değişiklik (Plan B
    — structured ön-eleme + Gemini Flash kategori/severity sentezi).
"""
from __future__ import annotations

from datetime import datetime
from sqlalchemy import (
    Column,
    String,
    DateTime,
    BigInteger,
    Integer,
    Float,
    Text,
    UniqueConstraint,
)

from core.database import Base


class WatchlistItem(Base):
    __tablename__ = "watchlist_items"
    __table_args__ = (
        UniqueConstraint("user_id", "symbol", name="uq_watchlist_user_symbol"),
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    user_id = Column(Integer, nullable=False, index=True)
    symbol = Column(String(16), nullable=False)
    category = Column(String(16), nullable=False, index=True)  # 'long_term' | 'swing'
    avg_cost = Column(Float, nullable=True)
    qty = Column(Float, nullable=True)
    notes = Column(Text, nullable=True)
    last_dossier_at = Column(DateTime(timezone=True), nullable=True)
    last_trigger_check_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=datetime.utcnow)


class DossierDiff(Base):
    __tablename__ = "dossier_diffs"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    user_id = Column(Integer, nullable=False, index=True)
    symbol = Column(String(16), nullable=False, index=True)
    prev_snapshot_id = Column(BigInteger, nullable=True)
    curr_snapshot_id = Column(BigInteger, nullable=False)
    diff_type = Column(String(32), nullable=False)  # decision_shift | target_change | trigger_near | risk_escalation | thesis_change
    severity = Column(String(8), nullable=False)  # low | mid | high
    summary = Column(Text, nullable=False)
    details = Column(Text, nullable=True)  # JSON-encoded structured diff
    sent_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=datetime.utcnow, index=True)
