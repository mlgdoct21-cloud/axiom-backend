from sqlalchemy import Column, BigInteger, String, Numeric, DateTime, JSON, Index
from sqlalchemy.sql import func
from core.database import Base


class EtfFlowCache(Base):
    """
    BTC/ETH spot ETF günlük net flow cache.
    Multi-source scraper günlük çalışır, son successful entry serve edilir.
    """
    __tablename__ = "etf_flow_cache"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    symbol = Column(String(8), nullable=False, index=True)  # 'BTC' or 'ETH'
    net_flow_usd = Column(Numeric(20, 2), nullable=False)
    net_flow_coins = Column(Numeric(20, 4), nullable=False)
    spot_price = Column(Numeric(20, 2), nullable=True)
    total_aum_usd = Column(Numeric(20, 2), nullable=True)
    total_holdings_coins = Column(Numeric(20, 4), nullable=True)
    source = Column(String(32), nullable=False)
    raw_data = Column(JSON, nullable=True)
    scraped_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("idx_etf_symbol_scraped", "symbol", "scraped_at"),
    )

    def __repr__(self):
        return (
            f"<EtfFlowCache {self.symbol} "
            f"${self.net_flow_usd:,.0f} / {self.net_flow_coins:,.2f} "
            f"({self.source} @ {self.scraped_at})>"
        )
