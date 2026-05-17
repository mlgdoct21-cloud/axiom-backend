"""Kurumsal Sentez scheduler — Commit 3.

coinglass_scheduler.py (cron supervisor) forku. İki cadence tek
supervisor'da:
  1. POLL (default 3h): kaynakları çek → store'a idempotent ingest
     (S1c accumulation — İş Yatırım feed ~1 gün derinlik bu yüzden şart).
  2. HAFTALIK (Pazartesi 08:30 Europe/Istanbul): birikmiş pencereden
     synthesize_week → broadcast (kill-switch default OFF).

Fail policy: kaynak başına + supervisor genelinde try/except + log;
exception ile supervisor kırılmaz. asyncio.CancelledError → temiz çıkış.
"""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, time, timedelta, timezone
from typing import Optional

from core.logger import get_logger
from services.corporate_sources import (
    ark_csv,
    blackrock_html,
    isyatirim_rss,
    jpm_eotm_html,
    mahfi_rss,
    overshoot_rss,
    radar_rss,
)
from services.corporate_sources.store import (
    ingest_holdings_snapshot,
    ingest_posts,
    normalize_post,
)
from services.corporate_synthesis import synthesize_week, week_event_id
from services.corporate_broadcaster import broadcast_synthesis_safe

logger = get_logger("corporate.scheduler")

_TR_TZ = timezone(timedelta(hours=3))  # Türkiye kalıcı UTC+3 (tzdata yok)
_PUBLISH_TIME = time(8, 30)
CORP_POLL_INTERVAL = int(os.getenv("CORP_POLL_INTERVAL_SECONDS", "10800"))  # 3h

# Bu süreçte hangi haftayı zaten sentezlemeye çalıştık (tekrar Gemini'yi
# tetiklememek için; synthesize_week zaten idempotent ama gereksiz SELECT'i
# de azaltır).
_last_synth_week_eid: Optional[str] = None


# ---------- Poll ----------

async def _poll_rss(mod, source: str, kind: str) -> dict:
    try:
        etag, lm = await mod.read_source_state()
        body, meta = await mod.fetch_feed(etag=etag, last_modified=lm)
        if body is None:
            return {"source": source, "status": meta.get("status"), "ingested": 0}
        posts = mod.parse_feed(body)
        rows = [normalize_post(source, kind, p) for p in posts]
        res = await ingest_posts(rows)
        await mod.write_source_state(
            meta.get("etag"), meta.get("last_modified")
        )
        return {"source": source, "parsed": len(posts), **res}
    except Exception as e:  # noqa: BLE001
        logger.warning(f"poll {source} failed: {e}")
        return {"source": source, "error": str(e)}


async def _poll_ark() -> dict:
    ok = 0
    for fund in ark_csv.FUND_FILES:
        try:
            body, _ = await ark_csv.fetch_holdings(fund)
            if not body:
                continue
            snap = ark_csv.parse_holdings(body, fund)
            if await ingest_holdings_snapshot(snap):
                ok += 1
        except Exception as e:  # noqa: BLE001
            logger.info(f"poll ark {fund} skip: {e}")
    return {"source": "ark", "funds_ingested": ok}


async def _poll_radar() -> dict:
    """Tema-radar kaynakları (yalnız başlık; gövde yok; kind='radar').
    Her kaynak bağımsız fail-soft. Sentez üretmez — live_block besler."""
    detail: dict = {}
    for key in radar_rss.RADAR_FEEDS:
        try:
            etag, lm = await radar_rss.read_source_state(key)
            body, meta = await radar_rss.fetch_feed(
                key, etag=etag, last_modified=lm
            )
            if body is None:
                detail[key] = {"status": meta.get("status"), "ingested": 0}
                continue
            posts = radar_rss.parse_feed(body, key)
            rows = [normalize_post(f"radar_{key}", "radar", p) for p in posts]
            res = await ingest_posts(rows)
            await radar_rss.write_source_state(
                meta.get("etag"), meta.get("last_modified"), key
            )
            detail[key] = {"parsed": len(posts), **res}
        except Exception as e:  # noqa: BLE001
            logger.warning(f"poll radar {key} failed: {e}")
            detail[key] = {"error": str(e)}
    return {"source": "radar", **detail}


async def _poll_once() -> list[dict]:
    out = []
    out.append(await _poll_rss(mahfi_rss, "mahfi", "article"))
    out.append(await _poll_rss(isyatirim_rss, "isyatirim", "report"))
    # tooze: Substack Cloudflare datacenter-IP'yi 403'lüyor → headless
    # tier'a (S3) ertelendi; adapter dosyası latent infra olarak duruyor.
    out.append(await _poll_rss(overshoot_rss, "overshoot", "essay"))
    out.append(await _poll_rss(blackrock_html, "blackrock", "commentary"))
    out.append(await _poll_rss(jpm_eotm_html, "jpm", "eotm"))
    out.append(await _poll_ark())
    out.append(await _poll_radar())
    logger.info(f"corp poll: {out}")
    return out


# ---------- Weekly trigger ----------

def _week_start_for(now_utc: datetime):
    """Bu haftanın sentez penceresi başlangıç Pazartesi'si (corporate_
    synthesis._week_bounds ile aynı mantık: önceki Pzt 08:30 TR)."""
    now_tr = now_utc.astimezone(_TR_TZ)
    this_mon = (now_tr - timedelta(days=now_tr.weekday())).date()
    this_0830 = datetime.combine(this_mon, _PUBLISH_TIME, _TR_TZ)
    prev_0830 = this_0830 - timedelta(days=7)
    return prev_0830.date(), this_0830.astimezone(timezone.utc)


def _next_weekly_tick(now_utc: datetime) -> tuple[float, datetime]:
    """Bir sonraki Pazartesi 08:30 TR'nin UTC'si + saniye."""
    now_tr = now_utc.astimezone(_TR_TZ)
    days_ahead = (7 - now_tr.weekday()) % 7  # 0 = bugün Pazartesi
    cand = datetime.combine(
        (now_tr + timedelta(days=days_ahead)).date(), _PUBLISH_TIME, _TR_TZ
    )
    if cand <= now_tr:
        cand += timedelta(days=7)
    cand_utc = cand.astimezone(timezone.utc)
    return (cand_utc - now_utc).total_seconds(), cand_utc


async def _maybe_weekly() -> None:
    """Bu haftanın Pzt 08:30 TR'si geçtiyse ve bu hafta henüz
    sentezlenmediyse synthesize_week + broadcast (kill-switch OFF)."""
    global _last_synth_week_eid
    now_utc = datetime.now(timezone.utc)
    week_start, this_0830_utc = _week_start_for(now_utc)
    if now_utc < this_0830_utc:
        return  # bu haftanın yayın anı henüz gelmedi
    eid = week_event_id(week_start)
    if eid == _last_synth_week_eid:
        return  # bu süreçte zaten denendi
    logger.info(f"corp weekly trigger: week_start={week_start} eid={eid}")
    results = await synthesize_week()  # idempotent; varsa cheap-skip
    _last_synth_week_eid = eid
    for r in results:
        if r.written or r.skipped and r.reason == "already exists (force=True ile yenile)":
            await broadcast_synthesis_safe(r.event_id, r.tier)


# ---------- Supervisor ----------

async def corporate_supervisor() -> None:
    logger.info(
        f"Corporate scheduler started — poll {CORP_POLL_INTERVAL}s, "
        f"weekly Pazartesi 08:30 TR (broadcast kill-switch "
        f"CORPORATE_SYNTH_BROADCAST_ENABLED default OFF)"
    )
    # Startup catch-up: hemen poll + (yayın anı geçtiyse) sentez.
    try:
        await _poll_once()
        await _maybe_weekly()
    except asyncio.CancelledError:
        return
    except Exception as e:  # noqa: BLE001
        logger.error(f"corp startup catch-up error: {e}")

    while True:
        try:
            now = datetime.now(timezone.utc)
            wsec, wtick = _next_weekly_tick(now)
            sleep_s = min(float(CORP_POLL_INTERVAL), wsec)
            logger.info(
                f"corp next: poll≤{CORP_POLL_INTERVAL}s | weekly "
                f"{wtick.isoformat()} ({wsec/3600:.1f}h) | sleep {sleep_s/3600:.2f}h"
            )
            await asyncio.sleep(sleep_s)
            await _poll_once()
            await _maybe_weekly()
        except asyncio.CancelledError:
            logger.info("corp scheduler cancelled")
            break
        except Exception as e:  # noqa: BLE001
            logger.error(f"corp loop error: {e}; continuing")
            await asyncio.sleep(300)
