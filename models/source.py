from sqlalchemy import Column, Integer, String, Boolean
from core.database import Base

class Source(Base):
    __tablename__ = "sources"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    url = Column(String, unique=True, nullable=False)
    is_active = Column(Boolean, default=True)
