from sqlalchemy import Column, Integer, String, Float, DateTime, ForeignKey, Boolean, Text, JSON
from sqlalchemy.sql import func
from core.database import Base


class SignalHistory(Base):
    __tablename__ = "signal_history"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    symbol = Column(String(20), nullable=False, index=True)
    signal_type = Column(String(20), nullable=False)  # "BUY", "SELL", "HOLD"
    confidence = Column(Float, nullable=False)  # 0.0 to 1.0
    reasoning = Column(Text, nullable=False)  # Explanation of why this signal
    indicators = Column(JSON, nullable=True)  # {"rsi": 65.5, "macd": 0.32, ...}
    price_at_signal = Column(Float, nullable=False)  # Price when signal was generated

    # Action tracking
    action_taken = Column(Boolean, default=False)  # Did user act on this signal?
    position_id = Column(Integer, ForeignKey("positions.id"), nullable=True)  # Link to position if acted upon

    timestamp = Column(DateTime(timezone=True), server_default=func.now(), index=True)

    def __repr__(self):
        return f"<SignalHistory {self.id}: {self.signal_type} {self.symbol} @ {self.confidence*100:.0f}%>"
