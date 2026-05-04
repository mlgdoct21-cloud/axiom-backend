"""
CryptoQuant On-Chain Alert Engine — threshold-based push notifications.

Watches the latest snapshot every 30 minutes and fires Telegram alerts to
premium/advance subscribers when one of 5 critical thresholds breaches.
Uses 6-hour per-alert cooldown to prevent spam (the same metric crossing
the same threshold should not fire repeatedly).

Also exposes morning_briefing() — a daily 09:00 TR digest with the
current Axiom Score, top 3 movers, and a 1-paragraph Turkish narrative.
"""
import asyncio
from datetime import datetime, timezone, timedelta
from typing import Optional

from sqlalchemy.future import select
from core.database import AsyncSessionLocal
from core.logger import get_logger
from models.user import User
from services.cryptoquant_service import get_onchain_snapshot
from services.telegram_bot import send_telegram_message

logger = get_logger("cryptoquant_alerts")

# ── Cooldown state — in-memory (restart resets, ok for now) ──────────────────
# {alert_key: unix_ts_resume_at}
_alert_cooldowns: dict[str, float] = {}
_ALERT_COOLDOWN = timedelta(hours=6)

# Daily-budget per user (in-memory, restart resets at UTC midnight)
# {(date_str, user_id): count}
_alerts_sent_today: dict[tuple[str, int], int] = {}


def _today_utc_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _get_daily_budget(tier: str) -> int:
    if tier == "premium":
        return 3
    if tier == "advance":
        return 9999
    return 0  # free: no alerts


def _is_in_cooldown(alert_key: str) -> bool:
    import time
    ts = _alert_cooldowns.get(alert_key, 0)
    return time.time() < ts


def _set_cooldown(alert_key: str) -> None:
    import time
    _alert_cooldowns[alert_key] = time.time() + _ALERT_COOLDOWN.total_seconds()


# ── Alert definitions ─────────────────────────────────────────────────────────

def _check_thresholds(snap: dict) -> list[dict]:
    """
    Returns a list of triggered alerts {key, severity, title, body}.
    severity: 'urgent' | 'attention' | 'info'
    """
    triggered = []
    sigs = snap.get("signals", {})

    # 1. Borsa netflow spike
    nf = snap.get("exchange_netflow")
    if nf:
        val = nf["netflow_total"]
        if val < -10000:
            triggered.append({
                "key": "netflow_outflow_spike",
                "severity": "info",
                "title": "🟢 Büyük Borsa Çıkışı",
                "body": f"Son 24 saatte borsalardan <b>{abs(val):,.0f} BTC</b> çıktı — balinalar biriktiriyor!",
            })
        elif val > 10000:
            triggered.append({
                "key": "netflow_inflow_spike",
                "severity": "urgent",
                "title": "⚠️ Büyük Borsa Girişi",
                "body": f"Son 24 saatte borsalara <b>{val:,.0f} BTC</b> girdi — satış baskısı oluşabilir, dikkat!",
            })

    # 2. Whale ratio yüksek
    wr = snap.get("whale_ratio")
    if wr and wr["whale_ratio"] >= 0.85:
        triggered.append({
            "key": "whale_ratio_critical",
            "severity": "attention",
            "title": "🐋 Balina Alarmı",
            "body": f"Borsa balina oranı <b>{wr['whale_ratio']:.2f}</b> — büyük oyuncular borsalarda aktif. Volatilite beklenebilir.",
        })

    # 3. Leverage kritik
    lev = snap.get("leverage_ratio")
    if lev and lev["leverage_ratio"] > 0.36:
        triggered.append({
            "key": "leverage_critical",
            "severity": "urgent",
            "title": "⚡ Kaldıraç Alarmı",
            "body": (
                f"Tahmini kaldıraç oranı <b>{lev['leverage_ratio']:.3f}</b> — kritik seviyede. "
                "Ani fiyat hareketi tasfiye dalgası tetikleyebilir. "
                "Stop-loss koymadan işlem açmayın!"
            ),
        })

    # 4. Funding extreme
    fr = snap.get("funding_rates")
    if fr:
        avg = fr["avg_24h"]
        if avg > 0.0005:
            triggered.append({
                "key": "funding_long_extreme",
                "severity": "attention",
                "title": "💸 Aşırı Long Pozisyon",
                "body": (
                    f"Funding ortalama <b>{avg*100:+.4f}%</b> (24s) — long pozisyonlar baskın. "
                    "Ters köşe satış hareketi gelebilir."
                ),
            })
        elif avg < -0.0005:
            triggered.append({
                "key": "funding_short_extreme",
                "severity": "info",
                "title": "💡 Short Sıkışması",
                "body": (
                    f"Funding ortalama <b>{avg*100:+.4f}%</b> (24s) — short pozisyonlar ödüyor. "
                    "Bears tükeniyor — sıkışma ralliği fırsatı olabilir."
                ),
            })

    # 5. MPI aşırı satış
    mpi = snap.get("mpi")
    if mpi and mpi["mpi"] > 2:
        triggered.append({
            "key": "mpi_high",
            "severity": "attention",
            "title": "⛏️ Madenci Satış Baskısı",
            "body": (
                f"MPI <b>{mpi['mpi']:+.2f}</b> — madenciler tarihsel ortalamanın çok üstünde "
                "satış yapıyor. Kısa vadeli baskı olabilir."
            ),
        })

    return triggered


# ── Alert dispatcher ──────────────────────────────────────────────────────────

async def sweep_and_dispatch() -> dict:
    """Single sweep: fetch snapshot, check thresholds, fan-out to subscribers
    that haven't hit their daily budget. Returns summary stats."""
    try:
        snap = await get_onchain_snapshot("BTC")
    except Exception as e:
        logger.error(f"alert sweep: snapshot fetch failed: {e}")
        return {"error": "snapshot_fetch_failed"}

    if not snap or snap.get("error"):
        return {"error": snap.get("error", "no_snapshot")}

    triggered = _check_thresholds(snap)
    if not triggered:
        return {"triggered": 0, "sent": 0}

    # Filter out cooled-down alerts
    fresh = [a for a in triggered if not _is_in_cooldown(a["key"])]
    if not fresh:
        return {"triggered": len(triggered), "sent": 0, "all_in_cooldown": True}

    # Mark cooldowns first (atomic-ish so concurrent sweeps don't double-fire)
    for a in fresh:
        _set_cooldown(a["key"])

    # Load eligible users
    try:
        async with AsyncSessionLocal() as session:
            all_users = list((await session.execute(select(User))).scalars().all())
    except Exception as e:
        logger.error(f"alert sweep: user query failed: {e}")
        return {"error": "user_query_failed"}

    eligible = []
    today = _today_utc_str()
    for u in all_users:
        tier = (getattr(u, "tier", "free") or "free").lower()
        budget = _get_daily_budget(tier)
        used = _alerts_sent_today.get((today, int(u.telegram_id)), 0)
        if budget > 0 and used < budget:
            eligible.append((u, tier, budget, used))

    if not eligible:
        logger.info(f"alert sweep: {len(fresh)} alerts triggered, no eligible users")
        return {"triggered": len(fresh), "sent": 0, "no_eligible_users": True}

    sent_count = 0
    fail_count = 0
    for alert in fresh:
        msg = _format_alert(alert, snap)
        for u, tier, budget, _used in eligible:
            uid = int(u.telegram_id)
            current_used = _alerts_sent_today.get((today, uid), 0)
            if current_used >= budget:
                continue
            try:
                send_telegram_message(int(u.telegram_id), msg)
                _alerts_sent_today[(today, uid)] = current_used + 1
                sent_count += 1
            except Exception as e:
                logger.warning(f"alert send fail for {u.telegram_id}: {e}")
                fail_count += 1

    logger.info(f"alert sweep: {len(fresh)} alert(s), {sent_count} sent, {fail_count} failed")
    return {"triggered": len(fresh), "sent": sent_count, "failed": fail_count}


def _format_alert(alert: dict, snap: dict) -> str:
    score = snap.get("axiom_score")
    zone = snap.get("score_zone_tr", "")
    lines = [
        f"🚨 <b>ON-CHAIN ALARMI</b>",
        "",
        f"{alert['title']}",
        "",
        alert["body"],
    ]
    if score is not None:
        lines.append("")
        lines.append(f"📊 Genel: Axiom Skor <b>{score:.0f}/100</b> {zone}")
    lines.append("")
    lines.append("<i>/onchain — Tam snapshot · /upgrade — Daha fazla alert</i>")
    return "\n".join(lines)


# ── Morning briefing ──────────────────────────────────────────────────────────

async def morning_briefing() -> dict:
    """Compose + fan out a daily morning briefing to premium/advance users.
    Triggered by scheduler at 06:00 UTC (09:00 TR)."""
    try:
        snap = await get_onchain_snapshot("BTC")
    except Exception as e:
        logger.error(f"briefing: snapshot fetch failed: {e}")
        return {"error": "snapshot_fetch_failed"}

    if not snap or snap.get("error"):
        return {"error": snap.get("error", "no_snapshot")}

    msg = _format_briefing(snap)

    try:
        async with AsyncSessionLocal() as session:
            all_users = list((await session.execute(select(User))).scalars().all())
    except Exception as e:
        logger.error(f"briefing: user query failed: {e}")
        return {"error": "user_query_failed"}

    paying = [
        u for u in all_users
        if (getattr(u, "tier", "free") or "free").lower() in ("premium", "advance")
    ]

    sent = fail = 0
    for u in paying:
        try:
            send_telegram_message(int(u.telegram_id), msg)
            sent += 1
        except Exception as e:
            logger.warning(f"briefing send fail for {u.telegram_id}: {e}")
            fail += 1

    logger.info(f"morning briefing: {sent}/{len(paying)} sent, {fail} failed")
    return {"sent": sent, "failed": fail, "eligible": len(paying)}


def _format_briefing(snap: dict) -> str:
    score = snap.get("axiom_score")
    zone = snap.get("score_zone_tr", "")
    summary = snap.get("score_summary", "")
    breakdown = snap.get("score_breakdown", [])

    positives = sorted(
        [b for b in breakdown if b.get("contribution", 0) > 0],
        key=lambda x: -x["contribution"],
    )[:3]
    negatives = sorted(
        [b for b in breakdown if b.get("contribution", 0) < 0],
        key=lambda x: x["contribution"],
    )[:3]

    today_str = datetime.now(timezone.utc).strftime("%-d %B %Y")
    # Try TR month names for cleaner display
    month_tr = {
        "January": "Ocak", "February": "Şubat", "March": "Mart", "April": "Nisan",
        "May": "Mayıs", "June": "Haziran", "July": "Temmuz", "August": "Ağustos",
        "September": "Eylül", "October": "Ekim", "November": "Kasım", "December": "Aralık",
    }
    for en, tr in month_tr.items():
        today_str = today_str.replace(en, tr)

    lines = [
        "☀️ <b>AXIOM SABAH BRİFİNGİ</b>",
        f"📅 {today_str} · BTC On-Chain",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        "",
    ]

    if score is not None:
        lines.append(f"🎯 <b>AXIOM SKOR: {score:.0f}/100</b>  {zone}")
        if summary:
            lines.append(f"<i>{summary}</i>")
        lines.append("")

    if positives:
        lines.append("✅ <b>GÜÇ VEREN SİNYALLER:</b>")
        for p in positives:
            lines.append(f"  +{p['contribution']:>2d}  {p['label_tr']}")
        lines.append("")

    if negatives:
        lines.append("⚠️ <b>BASKI YAPAN SİNYALLER:</b>")
        for n in negatives:
            lines.append(f"  {n['contribution']:>3d}  {n['label_tr']}")
        lines.append("")

    # Auto-narrative based on dominant signal balance
    pos_count = len(positives)
    neg_count = len(negatives)
    if pos_count > neg_count and score and score >= 70:
        narrative = (
            "Uzun vadeli tabloda akıllı para birikim yapıyor. "
            "Ancak kısa vadede aceleci hareket etmeyin — geri çekilmeleri "
            "fırsat olarak değerlendirin."
        )
    elif neg_count > pos_count and score and score <= 40:
        narrative = (
            "Satış baskısı baskın — yeni long pozisyon açmaktan kaçının, "
            "stop-loss seviyelerinizi sıkı tutun. Toparlanma için akıllı "
            "para sinyallerinin değişmesini bekleyin."
        )
    elif score and score >= 70:
        narrative = "Genel tablo olumlu, ama her sinyali izlemeye devam edin."
    elif score and score <= 40:
        narrative = "Risk yönetimi öncelikli — pozisyon büyüklüğünü düşük tutun."
    else:
        narrative = (
            "Karışık tablo: kuvvetler dengeli. Belirgin bir trend için "
            "yön sinyalini bekleyin."
        )

    lines.extend([
        "💬 <b>Axiom Yorumu:</b>",
        f"<i>{narrative}</i>",
        "",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        "/onchain — Detaylı snapshot",
    ])

    return "\n".join(lines)
