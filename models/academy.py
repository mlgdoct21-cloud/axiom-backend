"""Akademi ilerleme modeli — Faz 1 Opsiyon Akademisi.

Sadece kullanıcı ilerlemesi DB'de tutulur (lesson tamamlama + quiz skor).
Müfredat içeriği `data/academy/{curriculum,glossary}.yaml` statik dosyalarından
yüklenir — runtime AI üretmez.
"""
from sqlalchemy import (
    Column,
    Integer,
    String,
    DateTime,
    UniqueConstraint,
    Index,
)
from sqlalchemy.sql import func
from core.database import Base


class UserAcademyProgress(Base):
    __tablename__ = "user_academy_progress"
    __table_args__ = (
        UniqueConstraint("user_id", "lesson_id", name="uq_acad_user_lesson"),
        Index("ix_acad_user", "user_id"),
    )

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    user_id = Column(Integer, nullable=False, index=True)
    # Lesson slug formatı "M1L1", "M2L3" — curriculum.yaml id alanı
    lesson_id = Column(String(16), nullable=False)
    # Quiz cevabı doğru ise 1, yanlış ise 0, quiz cevaplanmadan tamamlandıysa NULL
    quiz_score = Column(Integer, nullable=True)
    attempts = Column(Integer, nullable=False, default=1, server_default="1")
    completed_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
