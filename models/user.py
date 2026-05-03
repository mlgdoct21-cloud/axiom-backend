from sqlalchemy import Column, Integer, String, Boolean, DateTime
from sqlalchemy.sql import func
from core.database import Base

class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    telegram_id = Column(String(20), unique=True, index=True, nullable=False)
    username = Column(String(32), nullable=True)
    # Kullanıcının aktif olup olmadığını tutar
    is_active = Column(Boolean, default=True)
    # Hangi etiketlerle ilgileniyor (Örn: "BTC,Apple,SEC") - virgülle ayrılmış string
    tags = Column(String(500), default="")
    # Raporlama tercihi: "realtime" (anlık) veya "digest" (günlük toplu)
    report_mode = Column(String(20), default="digest")
    # Eğer "digest" ise hangi saatte rapor istiyor (Örn: "08:00,18:00")
    report_hours = Column(String(100), default="08:00")
    # Kullanıcının serbest girdiği takip kelimeleri (Örn: "AAPL,Tesla,Nvidia")
    custom_follows = Column(String(1000), default="")
    # Subscription tier: free | premium | advance.
    # Free → macro broadcasts gated by 5-min delay + watermark prefix.
    # Premium / advance → instant, no watermark.
    tier = Column(String(20), nullable=False, default="free")
    # Stripe billing handles — populated by services/stripe_billing on the
    # first checkout session and updated by webhook events. Both nullable
    # because free-tier users never touch Stripe.
    stripe_customer_id = Column(String(64), nullable=True)
    stripe_subscription_id = Column(String(64), nullable=True)
    subscription_status = Column(String(32), nullable=True)  # active|past_due|canceled|...

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())
