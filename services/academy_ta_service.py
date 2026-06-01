"""Teknik Analiz Akademisi — statik içerik yükleyici + tier gate.

Müfredat ve sözlük `data/academy/{ta_curriculum,ta_glossary}.yaml`
dosyalarından bir kez (modül import'unda) yüklenir ve in-memory önbelleğe
alınır. Runtime AI üretmez — dondurulmuş statik.

Opsiyon akademisindeki `academy_service` ile birebir API paritesi:
get_curriculum_summary, get_module, get_lesson, get_glossary, search_glossary,
get_glossary_term — track="ta" için ayrı namespace.

Tier gate ilkesi:
- Sözlüğün tamamı tüm tier'lara açık (M5L1 dahil).
- Modül `tier_required: free` → herkese tam içerik.
- Modül `tier_required: premium/advance` → free kullanıcıya teaser.
- DERS-SEVİYESİ tier override: bir lesson `tier_required: free` taşıyorsa
  modül premium olsa bile o ders açıktır (M5L1 doji = free tadımlık).
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import yaml

from core.logger import get_logger

logger = get_logger("academy_ta_service")

_DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "academy"
_CURRICULUM_PATH = _DATA_DIR / "ta_curriculum.yaml"
_GLOSSARY_PATH = _DATA_DIR / "ta_glossary.yaml"

_TIER_RANK = {"free": 0, "premium": 1, "advance": 2}


def _load_yaml(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} root mapping olmalı")
    return data


try:
    _CURRICULUM: dict = _load_yaml(_CURRICULUM_PATH)
    _GLOSSARY: dict = _load_yaml(_GLOSSARY_PATH)
    logger.info(
        "TA Academy YAML yüklendi: %d modül, %d sözlük bölümü",
        len(_CURRICULUM.get("modules", [])),
        len([k for k in _GLOSSARY if k != "metadata"]),
    )
except FileNotFoundError as exc:
    logger.error("TA Academy YAML bulunamadı: %s", exc)
    _CURRICULUM = {"metadata": {}, "modules": []}
    _GLOSSARY = {"metadata": {}}


_LESSON_INDEX: dict[str, tuple[dict, dict]] = {}
for _mod in _CURRICULUM.get("modules", []):
    for _les in _mod.get("lessons", []):
        _LESSON_INDEX[_les["id"]] = (_mod, _les)


def _tier_at_least(user_tier: str, required: str) -> bool:
    return _TIER_RANK.get((user_tier or "free").lower(), 0) >= _TIER_RANK.get(
        (required or "free").lower(), 0
    )


def _effective_tier_required(module: dict, lesson: dict) -> str:
    """Ders kendi tier_required taşıyorsa o üstün gelir, yoksa modülün."""
    if lesson.get("tier_required"):
        return lesson["tier_required"]
    return module.get("tier_required", "free")


def _strip_correct(quiz: Optional[dict]) -> Optional[dict]:
    if not quiz:
        return None
    return {
        "question": quiz.get("question", ""),
        "options": [{"text": o.get("text", "")} for o in quiz.get("options", [])],
    }


def _serialize_lesson(module: dict, lesson: dict, *, user_tier: str) -> dict:
    required = _effective_tier_required(module, lesson)
    locked = not _tier_at_least(user_tier, required)
    if locked:
        return {
            "id": lesson["id"],
            "slug": lesson["slug"],
            "title": lesson["title"],
            "learning_objective": lesson.get("learning_objective", ""),
            "locked": True,
            "tier_hint": required,
        }
    return {
        "id": lesson["id"],
        "slug": lesson["slug"],
        "title": lesson["title"],
        "learning_objective": lesson.get("learning_objective", ""),
        "body": lesson.get("body", ""),
        "worked_examples": lesson.get("worked_examples", []),
        "navigation_link": lesson.get("navigation_link"),
        "navigation_note": lesson.get("navigation_note"),
        "quiz": _strip_correct(lesson.get("quiz")),
        "glossary_refs": lesson.get("glossary_refs", []),
        "tier_required": required,
        "locked": False,
    }


def _serialize_module(module: dict, *, user_tier: str) -> dict:
    required = module.get("tier_required", "free")
    # Modül "kilitli" kabul edilir AMA herhangi bir dersi açıksa kilit kalkar
    lessons_serialized = [_serialize_lesson(module, l, user_tier=user_tier) for l in module.get("lessons", [])]
    any_unlocked = any(not l.get("locked") for l in lessons_serialized)
    module_locked = not _tier_at_least(user_tier, required) and not any_unlocked
    return {
        "id": module["id"],
        "slug": module["slug"],
        "title": module["title"],
        "tagline": module.get("tagline", ""),
        "summary": module.get("summary", ""),
        "tier_required": required,
        "duration_min": module.get("duration_min"),
        "locked": module_locked,
        "lessons": lessons_serialized,
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def get_curriculum_summary(user_tier: str) -> dict:
    return {
        "metadata": _CURRICULUM.get("metadata", {}),
        "user_tier": (user_tier or "free").lower(),
        "track": "ta",
        "modules": [
            _serialize_module(m, user_tier=user_tier)
            for m in _CURRICULUM.get("modules", [])
        ],
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
    return {
        "module_id": module["id"],
        "module_title": module["title"],
        "lesson": _serialize_lesson(module, lesson, user_tier=user_tier),
    }


def get_glossary() -> dict:
    return _GLOSSARY


def get_glossary_term(slug: str) -> Optional[dict]:
    target = slug.lower().strip()
    for section_key, entries in _GLOSSARY.items():
        if section_key == "metadata" or not isinstance(entries, list):
            continue
        for entry in entries:
            if entry.get("slug", "").lower() == target:
                return {"section": section_key, **entry}
    return None


def search_glossary(query: str, limit: int = 8) -> list[dict]:
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
                for k in ("slug", "tr", "intuition", "one_liner")
            )
            if q in haystack:
                hits.append({"section": section_key, **entry})
                if len(hits) >= limit:
                    return hits
    return hits
