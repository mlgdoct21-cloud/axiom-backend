"""Kurumsal Sentez accumulation store — S1c.

Tüm prose/structured kaynakların ortak borusu. Adaptörler (mahfi_rss /
isyatirim_rss / ark_csv) DEĞİŞMEZ; bu modül onların üstünde duck-typed
bir normalize + idempotent UPSERT katmanı.

- prose → `corporate_posts`  (ON CONFLICT(source,external_id) DO UPDATE;
  first_seen_at KORUNUR → revizyon yakalanır ama re-broadcast engellenir).
- ARK structured → `corporate_holdings_snapshots` ((fund,as_of) idempotent).
- `read_window` → Commit 2 sentezi haftalık pencereyi buradan okur.

Fail policy: tüm DB ops sessiz skip + log; exception ile pipeline kırılmaz
(PPI 333-spam dersi). Scheduler (Commit 3) ve Gemini/broadcast burada YOK.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Optional

from sqlalchemy import bindparam, text

from core.database import engine
from core.logger import get_logger

logger = get_logger("corporate.store")


@dataclass
class CorporatePost:
    source: str
    external_id: str
    kind: str
    title: str
    link: str
    published: datetime          # tz-aware
    body_text: str
    truncated: bool
    author: str = ""
    meta: dict = field(default_factory=dict)


def _external_id(
    source: str, link: str, title: str, published: datetime
) -> str:
    """Idempotency anahtarı. Link varsa sha1(source|link); yoksa
    sha1(source|title|published_iso). 24 hex (çakışma pratikte imkânsız,
    TEXT kolon)."""
    if link:
        seed = f"{source}|{link}"
    else:
        seed = f"{source}|{title}|{published.isoformat()}"
    return hashlib.sha1(seed.encode("utf-8")).hexdigest()[:24]


def normalize_post(source: str, kind: str, obj: Any) -> CorporatePost:
    """DUCK-TYPED — MahfiPost/IsYatirimPost'u import ETMEDEN normalize eder.
    obj.title/.link/.published/.body_text/.truncated zorunlu; author ops."""
    title = (getattr(obj, "title", "") or "").strip()
    link = (getattr(obj, "link", "") or "").strip()
    published = getattr(obj, "published")
    return CorporatePost(
        source=source,
        external_id=_external_id(source, link, title, published),
        kind=kind,
        title=title,
        link=link,
        published=published,
        body_text=getattr(obj, "body_text", "") or "",
        truncated=bool(getattr(obj, "truncated", False)),
        author=(getattr(obj, "author", "") or "").strip(),
        meta={},
    )


_UPSERT_POST = text(
    """
    INSERT INTO corporate_posts
      (source, external_id, kind, title, link, published, body_text,
       truncated, author, meta, first_seen_at, fetched_at)
    VALUES
      (:source, :external_id, :kind, :title, :link, :published, :body_text,
       :truncated, :author, CAST(:meta AS JSONB), NOW(), NOW())
    ON CONFLICT (source, external_id) DO UPDATE SET
      title      = EXCLUDED.title,
      kind       = EXCLUDED.kind,
      link       = EXCLUDED.link,
      body_text  = EXCLUDED.body_text,
      truncated  = EXCLUDED.truncated,
      author     = EXCLUDED.author,
      meta       = EXCLUDED.meta,
      fetched_at = NOW()
    RETURNING (xmax = 0) AS inserted
    """
)


async def ingest_posts(rows: list[CorporatePost]) -> dict:
    """Idempotent UPSERT. Dönüş {'inserted','updated','skipped'}.
    DB hatası → log + kısmi sonuç (exception fırlatmaz)."""
    result = {"inserted": 0, "updated": 0, "skipped": 0}
    if not rows:
        return result
    try:
        async with engine.begin() as conn:
            for r in rows:
                if not r.title or not r.published:
                    result["skipped"] += 1
                    continue
                try:
                    inserted = (
                        await conn.execute(
                            _UPSERT_POST,
                            {
                                "source": r.source,
                                "external_id": r.external_id,
                                "kind": r.kind,
                                "title": r.title,
                                "link": r.link,
                                "published": r.published,
                                "body_text": r.body_text,
                                "truncated": r.truncated,
                                "author": r.author,
                                "meta": json.dumps(r.meta or {}),
                            },
                        )
                    ).scalar()
                    if inserted:
                        result["inserted"] += 1
                    else:
                        result["updated"] += 1
                except Exception as e:  # noqa: BLE001 — satır bazlı fail-soft
                    logger.warning(
                        f"ingest_posts row skip ({r.source}/{r.external_id}): {e}"
                    )
                    result["skipped"] += 1
    except Exception as e:  # noqa: BLE001 — DB/connection fail-soft
        logger.warning(f"ingest_posts DB error (yok sayılıyor): {e}")
    return result


_UPSERT_HOLDINGS = text(
    """
    INSERT INTO corporate_holdings_snapshots
      (source, fund, as_of, payload, holding_count, fetched_at)
    VALUES
      ('ark', :fund, :as_of, CAST(:payload AS JSONB), :hc, NOW())
    ON CONFLICT (fund, as_of) DO UPDATE SET
      payload       = EXCLUDED.payload,
      holding_count = EXCLUDED.holding_count,
      fetched_at    = NOW()
    """
)


def _holding_to_jsonable(h: Any) -> dict:
    d = dataclasses.asdict(h)
    for k, v in list(d.items()):
        if isinstance(v, (date, datetime)):
            d[k] = v.isoformat()
    return d


async def ingest_holdings_snapshot(snap: Any) -> bool:
    """ARK günlük snapshot UPSERT ((fund,as_of) idempotent). as_of None
    veya holding yok → skip+log. Hata → log, False."""
    fund = getattr(snap, "fund", None)
    as_of = getattr(snap, "as_of", None)
    holdings = getattr(snap, "holdings", None) or []
    if not fund or as_of is None or not holdings:
        logger.info(
            f"ingest_holdings skip (fund={fund} as_of={as_of} n={len(holdings)})"
        )
        return False
    payload = [_holding_to_jsonable(h) for h in holdings]
    try:
        async with engine.begin() as conn:
            await conn.execute(
                _UPSERT_HOLDINGS,
                {
                    "fund": str(fund).upper(),
                    "as_of": as_of,
                    "payload": json.dumps(payload),
                    "hc": len(payload),
                },
            )
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning(f"ingest_holdings DB error ({fund}/{as_of}): {e}")
        return False


_READ_WINDOW = text(
    """
    SELECT source, external_id, kind, title, link, published, body_text,
           truncated, author, meta, first_seen_at, fetched_at
      FROM corporate_posts
     WHERE source IN :srcs
       AND published >= :start
       AND published <  :end
     ORDER BY published DESC
    """
).bindparams(bindparam("srcs", expanding=True))


async def read_window(
    sources: list[str], start: datetime, end: datetime
) -> list[dict]:
    """[start, end) penceresindeki postlar (DESC). Commit 2 sentezi bunu
    okur. DB hatası → log + boş liste (fail-soft)."""
    if not sources:
        return []
    try:
        async with engine.begin() as conn:
            rows = (
                await conn.execute(
                    _READ_WINDOW,
                    {"srcs": sources, "start": start, "end": end},
                )
            ).mappings().all()
        return [dict(r) for r in rows]
    except Exception as e:  # noqa: BLE001
        logger.warning(f"read_window DB error (yok sayılıyor): {e}")
        return []
