from sqlalchemy import Column, Integer, String, Float, DateTime, ForeignKey, Boolean
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from core.database import Base


class Position(Base):
    __tablename__ = "positions"

    id = Column(Integer, primary_key=True, index=True)
    portfolio_id = Column(Integer, ForeignKey("portfolios.id"), nullable=False, index=True)
    symbol = Column(String(20), nullable=False, index=True)  # e.g., "AAPL", "BTC"
    quantity = Column(Float, nullable=False)  # Number of shares/units
    average_entry_price = Column(Float, nullable=False)  # Average entry price
    current_price = Column(Float, nullable=False)  # Latest price
    entry_date = Column(DateTime(timezone=True), nullable=False)
    exit_date = Column(DateTime(timezone=True), nullable=True)  # NULL if still open
    status = Column(String(20), default="open")  # "open" or "closed"

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    # Relationships
    portfolio = relationship("Portfolio", back_populates="positions")

    def calculate_pnl_amount(self) -> float:
        """Calculate P&L in currency"""
        return (self.current_price - self.average_entry_price) * self.quantity

    def calculate_pnl_percent(self) -> float:
        """Calculate P&L in percentage"""
        if self.average_entry_price == 0:
            return 0
        return ((self.current_price - self.average_entry_price) / self.average_entry_price) * 100

    def __repr__(self):
        return f"<Position {self.id}: {self.symbol} x{self.quantity} ({self.status})>"
