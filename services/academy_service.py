"""Opsiyon Akademisi Faz 1 — statik içerik yükleyici + ilerleme + tier gate.

Müfredat ve sözlük `data/academy/{curriculum,glossary}.yaml` dosyalarından
bir kez (modül import'unda) yüklenir ve in-memory önbelleğe alınır.
Runtime AI üretmez — içerik dondurulmuş statik.

Tier gating ilkesi:
- Sözlüğün tamamı tüm tier'lara açık.
- Modül `tier_required: free` → herkese tam içerik.
- Modül `tier_required: premium/advance` → free kullanıcıya yalnız
  başlık + tagline + summary + lesson özetleri "🔒 Premium" teaser
  olarak gider; body, worked_examples, quiz feedback gizlenir.
- Quiz `correct` flag'leri serializer'da client'a İADESİZ — yanıt
  POST /progress üzerinden server-side doğrulanır.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import yaml
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from core.database import engine
from core.logger import get_logger
from models.academy import UserAcademyProgress

logger = get_logger("academy_service")

_DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "academy"
_CURRICULUM_PATH = _DATA_DIR / "curriculum.yaml"
_GLOSSARY_PATH = _DATA_DIR / "glossary.yaml"

# Tier hiyerarşisi — yüksek erişen alt seviyeleri de görür.
_TIER_RANK = {"free": 0, "premium": 1, "advance": 2}


def _load_yaml(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} root must be a mapping, got {type(data)}")
    return data


try:
    _CURRICULUM: dict = _load_yaml(_CURRICULUM_PATH)
    _GLOSSARY: dict = _load_yaml(_GLOSSARY_PATH)
    logger.info(
        "Academy YAML yüklendi: %d modül, %d sözlük bölümü",
        len(_CURRICULUM.get("modules", [])),
        len([k for k in _GLOSSARY if k != "metadata"]),
    )
except FileNotFoundError as exc:
    # Uygulama yine de ayağa kalksın — endpoint'ler boş döner.
    logger.error("Academy YAML bulunamadı: %s", exc)
    _CURRICULUM = {"metadata": {}, "modules": []}
    _GLOSSARY = {"metadata": {}}


# Hızlı erişim için ders id → (module, lesson) indeksi.
_LESSON_INDEX: dict[str, tuple[dict, dict]] = {}
for _mod in _CURRICULUM.get("modules", []):
    for _les in _mod.get("lessons", []):
        _LESSON_INDEX[_les["id"]] = (_mod, _les)


def _tier_at_least(user_tier: str, required: str) -> bool:
    return _TIER_RANK.get((user_tier or "free").lower(), 0) >= _TIER_RANK.get(
        (required or "free").lower(), 0
    )


def _strip_correct(quiz: Optional[dict]) -> Optional[dict]:
    """Quiz'in doğru cevabını ve per-option feedback'leri client'a İADESİZ.

    Kullanıcı seçimini POST /progress üzerinden gönderir; server doğrular,
    feedback'i o cevaba göre döner.
    """
    if not quiz:
        return None
    options = []
    for opt in quiz.get("options", []):
        options.append({"text": opt.get("text", "")})
    return {
        "question": quiz.get("question", ""),
        "options": options,
    }


def _serialize_lesson(lesson: dict, *, locked: bool) -> dict:
    """Tek dersi response payload'ına çevir.

    locked=True → free kullanıcı premium derse erişiyor demektir; gövde gizli,
    yalnız teaser alanları döner.
    """
    if locked:
        return {
            "id": lesson["id"],
            "slug": lesson["slug"],
            "title": lesson["title"],
            "learning_objective": lesson.get("learning_objective", ""),
            "locked": True,
            "tier_hint": "premium",
        }
    return {
        "id": lesson["id"],
        "slug": lesson["slug"],
        "title": lesson["title"],
        "learning_objective": lesson.get("learning_objective", ""),
        "body": lesson.get("body", ""),
        "worked_examples": lesson.get("worked_examples", []),
        "horology_link": lesson.get("horology_link"),
        "horology_note": lesson.get("horology_note"),
        "quiz": _strip_correct(lesson.get("quiz")),
        "glossary_refs": lesson.get("glossary_refs", []),
        "locked": False,
    }


def _serialize_module(module: dict, *, user_tier: str) -> dict:
    required = module.get("tier_required", "free")
    locked = not _tier_at_least(user_tier, required)
    return {
        "id": module["id"],
        "slug": module["slug"],
        "title": module["title"],
        "tagline": module.get("tagline", ""),
        "summary": module.get("summary", ""),
        "tier_required": required,
        "duration_min": module.get("duration_min"),
        "locked": locked,
        "lessons": [
            _serialize_lesson(les, locked=locked)
            for les in module.get("lessons", [])
        ],
    }


# ---------------------------------------------------------------------------
# Public API — content
# ---------------------------------------------------------------------------


def get_curriculum_summary(user_tier: str) -> dict:
    """Tüm modüllerin tier-aware özetini döner. Quiz cevapları yok."""
    return {
        "metadata": _CURRICULUM.get("metadata", {}),
        "user_tier": (user_tier or "free").lower(),
        "modules": [
            _serialize_module(m, user_tier=user_tier)
            for m in _CURRICULUM.get("modules", [])
        ],
        "scenario_cards": _CURRICULUM.get("scenario_cards", []),
    }


def get_module(module_id: str, user_tier: str) -> Optional[dict]:
    for m in _CURRICULUM.get("modules", []):
        if m["id"].upper() == module_id.upper():
            return _serialize_module(m, user_tier=user_tier)
    return None


def get_lesson(lesson_id: str, user_tier: str) -> Optional[dict]:
    entry = _LESSON_INDEX.get(lesson_id.upper())
    if not entry:
        return None
    module, lesson = entry
    required = module.get("tier_required", "free")
    locked = not _tier_at_least(user_tier, required)
    return {
        "module_id": module["id"],
        "module_title": module["title"],
        "lesson": _serialize_lesson(lesson, locked=locked),
    }


def get_glossary() -> dict:
    """Sözlüğün tamamı — tüm tier'lara açık."""
    return _GLOSSARY


def get_glossary_term(slug: str) -> Optional[dict]:
    target = slug.lower().strip()
    for section_key, entries in _GLOSSARY.items():
        if section_key == "metadata":
            continue
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if entry.get("slug", "").lower() == target:
                return {"section": section_key, **entry}
    return None


def search_glossary(query: str, limit: int = 8) -> list[dict]:
    """Basit substring araması — slug, tr, intuition, one_liner."""
    q = (query or "").lower().strip()
    if not q:
        return []
    hits: list[dict] = []
    for section_key, entries in _GLOSSARY.items():
        if section_key == "metadata" or not isinstance(entries, list):
            continue
        for entry in entries:
            haystack = " ".join(
                str(entry.get(k, "")).lower()
                for k in ("slug", "tr", "intuition", "one_liner", "metaphor")
            )
            if q in haystack:
                hits.append({"section": section_key, **entry})
                if len(hits) >= limit:
                    return hits
    return hits


def get_scenario_cards() -> list[dict]:
    return _CURRICULUM.get("scenario_cards", [])


def get_theta_wheel_spec() -> dict:
    return _CURRICULUM.get("theta_wheel_v1", {})


# ---------------------------------------------------------------------------
# Public API — progress + quiz
# ---------------------------------------------------------------------------


@dataclass
class QuizResult:
    correct: bool
    feedback: str
    correct_option_index: int


def evaluate_quiz(lesson_id: str, option_index: int) -> Optional[QuizResult]:
    """Server-side quiz doğrulaması. Lesson bulunamazsa None."""
    entry = _LESSON_INDEX.get(lesson_id.upper())
    if not entry:
        return None
    _, lesson = entry
    quiz = lesson.get("quiz")
    if not quiz:
        return None
    options = quiz.get("options", [])
    if option_index < 0 or option_index >= len(options):
        return QuizResult(
            correct=False,
            feedback="Geçersiz seçenek.",
            correct_option_index=_find_correct_index(options),
        )
    chosen = options[option_index]
    correct = bool(chosen.get("correct", False))
    feedback = chosen.get("feedback", "")
    return QuizResult(
        correct=correct,
        feedback=feedback,
        correct_option_index=_find_correct_index(options),
    )


def _find_correct_index(options: list[dict]) -> int:
    for i, opt in enumerate(options):
        if opt.get("correct"):
            return i
    return -1


async def record_progress(
    db: AsyncSession,
    *,
    user_id: int,
    lesson_id: str,
    quiz_score: Optional[int],
) -> dict:
    """Idempotent ders tamamlama kaydı. Aynı kullanıcı + ders yine gelirse
    `attempts` artar, `quiz_score` MAX olur, `updated_at` yenilenir."""
    if lesson_id.upper() not in _LESSON_INDEX:
        raise ValueError(f"Unknown lesson: {lesson_id}")
    lesson_id_norm = lesson_id.upper()

    dialect = engine.dialect.name
    if dialect == "postgresql":
        stmt = (
            pg_insert(UserAcademyProgress)
            .values(
                user_id=user_id,
                lesson_id=lesson_id_norm,
                quiz_score=quiz_score,
                attempts=1,
            )
            .on_conflict_do_update(
                constraint="uq_acad_user_lesson",
                set_={
                    "attempts": UserAcademyProgress.__table__.c.attempts + 1,
                    "quiz_score": (
                        # COALESCE(GREATEST(existing, new), new)
                        # SQLAlchemy server-side ifadesi karmaşıklaşır;
                        # explicit fonksiyonla yapıyoruz.
                        UserAcademyProgress.__table__.c.quiz_score
                        if quiz_score is None
                        else UserAcademyProgress.__table__.c.quiz_score
                    ),
                    "updated_at": datetime.now(timezone.utc),
                },
            )
        )
        # quiz_score için GREATEST yaklaşımı: önce mevcut kaydı oku, sonra
        # max(old, new) hesapla. Tek upsert + sonradan UPDATE değil; sadece
        # UPSERT pencerede max'i set et.
        await db.execute(stmt)
        if quiz_score is not None:
            # quiz_score MAX'i: ikinci, küçük bir UPDATE.
            await db.execute(
                UserAcademyProgress.__table__.update()
                .where(UserAcademyProgress.user_id == user_id)
                .where(UserAcademyProgress.lesson_id == lesson_id_norm)
                .where(
                    (UserAcademyProgress.quiz_score.is_(None))
                    | (UserAcademyProgress.quiz_score < quiz_score)
                )
                .values(quiz_score=quiz_score)
            )
        await db.commit()
    else:
        # SQLite fallback — ORM yolu.
        existing = (
            await db.execute(
                select(UserAcademyProgress).where(
                    (UserAcademyProgress.user_id == user_id)
                    & (UserAcademyProgress.lesson_id == lesson_id_norm)
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            db.add(
                UserAcademyProgress(
                    user_id=user_id,
                    lesson_id=lesson_id_norm,
                    quiz_score=quiz_score,
                    attempts=1,
                )
            )
        else:
            existing.attempts = (existing.attempts or 0) + 1
            if quiz_score is not None and (
                existing.quiz_score is None or quiz_score > existing.quiz_score
            ):
                existing.quiz_score = quiz_score
            existing.updated_at = datetime.now(timezone.utc)
        await db.commit()

    return await get_progress(db, user_id=user_id)


async def get_progress(db: AsyncSession, *, user_id: int) -> dict:
    rows = (
        await db.execute(
            select(UserAcademyProgress).where(
                UserAcademyProgress.user_id == user_id
            )
        )
    ).scalars().all()
    by_lesson = {
        r.lesson_id: {
            "lesson_id": r.lesson_id,
            "quiz_score": r.quiz_score,
            "attempts": r.attempts,
            "completed_at": r.completed_at.isoformat()
            if r.completed_at
            else None,
            "updated_at": r.updated_at.isoformat() if r.updated_at else None,
        }
        for r in rows
    }
    # Modül-başına özet (kaç ders / kaç tamamlandı).
    module_summary = []
    for mod in _CURRICULUM.get("modules", []):
        lesson_ids = [les["id"] for les in mod.get("lessons", [])]
        total = len(lesson_ids)
        done = sum(1 for lid in lesson_ids if lid in by_lesson)
        module_summary.append(
            {
                "module_id": mod["id"],
                "total": total,
                "completed": done,
                "percent": int(round(100 * done / total)) if total else 0,
            }
        )
    return {
        "user_id": user_id,
        "lessons": list(by_lesson.values()),
        "modules": module_summary,
        "total_lessons": len(_LESSON_INDEX),
        "total_completed": len(by_lesson),
    }
