from sqlalchemy import Column, BigInteger, String, Text, DateTime, Numeric, UniqueConstraint, Index
from sqlalchemy.sql import func
from core.database import Base


class BistFinancial(Base):
    """Long-format BIST UFRS bilanço/gelir/nakit-akış satırı.

    isyatirimhisse.fetch_financials() çıktısının pivot-edilmiş hali:
    her satır tek bir (symbol, period, item_code) gözlemi.
    """
    __tablename__ = "bist_financials"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    symbol = Column(String(16), nullable=False, index=True)        # "ASELS"
    period = Column(String(8), nullable=False)                     # "2025/9"
    item_code = Column(String(16), nullable=False)                 # "1A", "1AA", "2A"
    item_name_tr = Column(Text, nullable=True)                     # "Dönen Varlıklar"
    item_name_en = Column(Text, nullable=True)                     # "CURRENT ASSETS"
    value = Column(Numeric(28, 2), nullable=True)                  # TL tutar
    currency = Column(String(4), nullable=False, default="TRY", server_default="TRY")
    financial_group = Column(String(4), nullable=False, default="1", server_default="1")
    source = Column(String(32), nullable=False, default="isyatirimhisse", server_default="isyatirimhisse")
    fetched_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        UniqueConstraint("symbol", "period", "item_code", "currency", "financial_group", name="uq_bist_fin_spi"),
        Index("ix_bist_fin_symbol_period", "symbol", "period"),
    )
