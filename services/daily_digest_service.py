"""
Daily Digest Service — "Günün Özeti ve Axiom Tespiti" 3 ana card.

V2: FMP-driven (Gemini-free). Ham piyasa datasıyla deterministik Türkçe metin
üretir — hızlı, ucuz, her zaman tutarlı.

3 card:
  1. RISK RADAR        — Makro riskler, volatilite (VIX + breaking news count)
  2. KANTITATIF ANALIZ — Sector top performers + earnings calendar
  3. PORTFÖY SINYAL    — Top movers + USDC/BTC ETF flow snapshot
"""
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_, func

import yfinance as yf

from models.news import NewsItem
from services.market_summary_service import (
    get_overnight_markets,
    get_sector_performance,
    get_premarket_movers,
    get_etf_flows,
)
from core.logger import get_logger

logger = get_logger("daily_digest_service")


class DailyDigestService:
    """FMP-driven 3-card digest service."""

    # ─── Helpers ──────────────────────────────────────────────────────────

    @staticmethod
    async def _get_urgent_news_count(db: AsyncSession, hours: int = 12) -> int:
        """Son N saatte kaç urgent haber çıkmış?"""
        try:
            cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
            stmt = select(func.count(NewsItem.id)).where(
                and_(
                    NewsItem.is_urgent == True,  # noqa: E712
                    NewsItem.created_at >= cutoff,
                )
            )
            result = await db.execute(stmt)
            return result.scalar() or 0
        except Exception as e:
            logger.warning(f"Urgent news count error: {e}")
            return 0

    @staticmethod
    async def _get_top_urgent_symbols(db: AsyncSession, limit: int = 3, hours: int = 12) -> List[str]:
        """Son saatlerdeki urgent haberlerden top semboller (frequency)."""
        try:
            cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
            stmt = (
                select(NewsItem.symbol, func.count(NewsItem.id).label("freq"))
                .where(
                    and_(
                        NewsItem.is_urgent == True,  # noqa: E712
                        NewsItem.created_at >= cutoff,
                        NewsItem.symbol.isnot(None),
                    )
                )
                .group_by(NewsItem.symbol)
                .order_by(func.count(NewsItem.id).desc())
                .limit(limit)
            )
            result = await db.execute(stmt)
            return [row[0] for row in result.all() if row[0]]
        except Exception as e:
            logger.warning(f"Top urgent symbols error: {e}")
            return []

    @staticmethod
    async def _get_vix() -> Optional[Dict[str, Any]]:
        """VIX (fear gauge) — FMP-first, yfinance fallback.

        Day 28 part 5: yfinance Railway'de bloklu (Day 4 BLS pattern, IP-based
        rate limit). FMP Premium aboneliğimiz `/stable/batch-quote?symbols=^VIX`
        ile sorunsuz dönüyor; yfinance lokal/dev fallback olarak kalıyor.
        """
        # Primary — FMP
        try:
            from services.market_summary_service import get_vix_quote_fmp
            fmp = await get_vix_quote_fmp()
            if fmp and fmp.get("current"):
                return fmp
        except Exception as e:
            logger.warning(f"VIX FMP fetch error: {e}")

        # Fallback — yfinance (lokal dev'de Railway dışında çalışır)
        try:
            vix = yf.Ticker("^VIX")
            data = vix.history(period="5d")
            if data.empty:
                return None
            current = float(data["Close"].iloc[-1])
            prev = float(data["Close"].iloc[-2]) if len(data) > 1 else current
            change_pct = ((current - prev) / prev * 100) if prev > 0 else 0
            if current > 30:
                status, color = "Yüksek (Panik)", "red"
            elif current > 20:
                status, color = "Orta", "yellow"
            elif current > 15:
                status, color = "Normal", "green"
            else:
                status, color = "Düşük (Sakin)", "green"
            return {
                "current": round(current, 2),
                "status": status,
                "color": color,
                "change_pct": round(change_pct, 2),
            }
        except Exception as e:
            logger.warning(f"VIX yfinance fallback error: {e}")
            return None

    # ─── Card builders ────────────────────────────────────────────────────

    @staticmethod
    def _build_risk_radar(
        vix: Optional[Dict[str, Any]],
        urgent_count: int,
        urgent_symbols: List[str],
        overnight: Dict[str, Any],
        onchain: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """RISK RADAR — makro riskler ve volatilite + BTC on-chain yön sinyali."""
        # Asya pazarındaki en büyük düşüşü bul
        asia_data = overnight.get("asia", []) if isinstance(overnight, dict) else []
        biggest_drop = None
        if asia_data:
            sorted_asia = sorted(asia_data, key=lambda x: x.get("change_pct", 0))
            if sorted_asia and sorted_asia[0].get("change_pct", 0) < -0.5:
                biggest_drop = sorted_asia[0]

        # On-chain yön sinyali (BTC) — SSoT: cryptoquant_service'in zaten
        # yorumladığı signals dict'ten oku. Burada tekrar threshold mantığı
        # YOK — eşikler bir tek _interpret_signals'da tanımlı, böylece
        # Daily Digest ile On-Chain modal AYNI verdict'i gösterir.
        netflow_part: Optional[str] = None
        funding_part: Optional[str] = None
        onchain_risk = False  # red flag: hem netflow hem funding BEARISH mi?
        signals = (onchain or {}).get("signals") or {}
        if signals:
            nf_sig = signals.get("exchange_netflow")
            if nf_sig:
                netflow_part = (
                    f"borsa akışı {nf_sig.get('value_str','')} "
                    f"({nf_sig.get('label_tr','')})"
                )
                if nf_sig.get("signal") == "BEARISH":
                    onchain_risk = True

            fr_sig = signals.get("funding_rates")
            if fr_sig:
                funding_part = (
                    f"funding {fr_sig.get('value_str','')} "
                    f"({fr_sig.get('label_tr','')})"
                )
                if fr_sig.get("signal") == "BEARISH":
                    onchain_risk = True

        # Metin oluştur (template-based, Türkçe). En değerli sinyal en önde.
        parts = []
        if vix and vix.get("current"):
            parts.append(f"VIX {vix['current']} ({vix['status']})")

        if netflow_part:
            parts.append(netflow_part)
        if funding_part:
            parts.append(funding_part)

        if biggest_drop:
            parts.append(f"{biggest_drop['label']} {biggest_drop['change_pct']:+.2f}%")

        if urgent_count >= 5:
            parts.append(f"son 12 saatte {urgent_count} acil haber")
        elif urgent_count >= 1:
            parts.append(f"{urgent_count} önemli gelişme takipte")

        if not parts:
            analysis = "Pazar göstergeleri sakin. Belirgin makro risk gözlenmiyor."
            color = "green"
        else:
            analysis = "Bugün dikkat: " + ", ".join(parts) + "."
            # En yüksek risk seviyesini belirle
            if vix and vix.get("color") == "red":
                color = "red"
            elif onchain_risk or biggest_drop:
                color = "yellow"
            elif urgent_count >= 5:
                color = "yellow"
            else:
                color = vix.get("color", "yellow") if vix else "yellow"

        # Semboller: VIX + on-chain varsa BTC + urgent semboller
        symbols = ["VIX"] if vix else []
        if netflow_part or funding_part:
            symbols.append("BTC")
        symbols.extend(urgent_symbols[:3])
        if biggest_drop:
            symbols.append(biggest_drop["label"])

        # Dedupe (sıra korunarak)
        seen = set()
        symbols = [s for s in symbols if not (s in seen or seen.add(s))]

        return {
            "title": "AXIOM RISK RADAR",
            "analysis": analysis,
            "symbols": symbols[:5],
            "color": color,
        }

    # _build_quant_analysis removed (Day 28 part 5).
    # Sektör rotasyonu zaten MiniSectorChip'te ayrı kart olarak gösteriliyor;
    # earnings sayısı MiniEarningsChip'te. Sektör baskısı (top/bottom) artık
    # frontend'de Risk Radar modal'ına entegre — orada faktör kartları arasında.

    @staticmethod
    def _build_portfolio_signal(
        movers: Dict[str, List[Dict[str, Any]]],
        etf_flows: Dict[str, Any],
    ) -> Dict[str, Any]:
        """PORTFÖY SINYAL — top movers + ETF flows."""
        gainers = movers.get("gainers", [])
        losers = movers.get("losers", [])

        parts = []
        symbols = []

        if gainers:
            top_gainer = gainers[0]
            parts.append(
                f"{top_gainer['symbol']} {top_gainer.get('change_pct', 0):+.1f}% liderlikte"
            )
            symbols.append(top_gainer["symbol"])

        if losers:
            top_loser = losers[0]
            parts.append(
                f"{top_loser['symbol']} {top_loser.get('change_pct', 0):+.1f}% baskıda"
            )
            symbols.append(top_loser["symbol"])

        # BTC ETF flow snapshot
        btc_flows = etf_flows.get("btc", {}) if isinstance(etf_flows, dict) else {}
        btc_volume = btc_flows.get("daily_volume", 0)
        if btc_volume > 0:
            volume_b = btc_volume / 1e9
            if volume_b >= 1:
                parts.append(f"BTC ETF günlük hacim ${volume_b:.1f}B")
                symbols.append("IBIT")

        if not parts:
            recommendation = "Pazar açılışı için stratejik bekleme önerilir."
            color = "blue"
        else:
            recommendation = "Akış: " + "; ".join(parts) + "."
            color = "blue"

        return {
            "title": "PORTFÖY SINYAL",
            "recommendation": recommendation,
            "symbols": symbols[:4],
            "color": color,
        }

    # ─── Public API ───────────────────────────────────────────────────────

    @staticmethod
    async def get_daily_digest(db: AsyncSession) -> Dict[str, Any]:
        """
        3 card'ı oluştur. Tüm FMP çağrıları paralel; bir tanesi düşse de diğerleri çalışır.
        """
        import asyncio

        try:
            # Per-task hard timeout — eski hata: bir yavaş bağımlılık (CryptoQuant
            # cold-cache, FMP 429) tüm gather'ı kilitliyordu; Risk Radar + Portföy
            # Sinyali UI'da boş kalıyordu (2026-05-13 incidence).
            # Şimdi her task sınırlı; biri timeout olursa `_safe` default'a düşer,
            # diğerleri etkilenmez.
            _TASK_TIMEOUT = 5.0

            async def _bounded(coro, label: str):
                try:
                    return await asyncio.wait_for(coro, timeout=_TASK_TIMEOUT)
                except asyncio.TimeoutError:
                    logger.warning(f"daily-digest task timeout: {label}")
                    return None
                except Exception as e:
                    logger.warning(f"daily-digest task error ({label}): {e}")
                    return None

            from services.market_summary_service import get_earnings_today
            from services.cryptoquant_service import get_onchain_snapshot

            results = await asyncio.gather(
                _bounded(DailyDigestService._get_urgent_news_count(db, hours=12),       "urgent_count"),
                _bounded(DailyDigestService._get_top_urgent_symbols(db, limit=3, hours=12), "urgent_symbols"),
                _bounded(get_overnight_markets(),                                          "overnight"),
                _bounded(get_sector_performance(),                                         "sectors"),
                _bounded(get_premarket_movers(limit=3),                                    "movers"),
                _bounded(get_etf_flows(),                                                  "etf_flows"),
                _bounded(get_earnings_today(limit=20),                                     "earnings"),
                _bounded(get_onchain_snapshot("BTC"),                                      "onchain"),
                return_exceptions=True,
            )

            def _safe(r, default):
                return default if isinstance(r, Exception) else (r if r is not None else default)

            urgent_count = _safe(results[0], 0)
            urgent_symbols = _safe(results[1], [])
            overnight = _safe(results[2], {"asia": [], "europe": [], "us_futures": []})
            sectors = _safe(results[3], [])
            movers = _safe(results[4], {"gainers": [], "losers": [], "actives": []})
            etf_flows = _safe(results[5], {"btc": {}, "eth": {}})
            earnings_list = _safe(results[6], [])
            onchain = _safe(results[7], None)

            # VIX (FMP-first, yfinance fallback) — yfinance arada 30s hang yapıyor
            vix = await _bounded(DailyDigestService._get_vix(), "vix")

            # 2 card oluştur (Kantitatif kaldırıldı — Day 28 part 5)
            risk_radar = DailyDigestService._build_risk_radar(
                vix=vix,
                urgent_count=urgent_count,
                urgent_symbols=urgent_symbols,
                overnight=overnight,
                onchain=onchain,
            )
            portfolio_signal = DailyDigestService._build_portfolio_signal(
                movers=movers,
                etf_flows=etf_flows,
            )

            # Sektör verisi artık Risk Radar modal'ı tarafından da kullanılıyor
            # (sektör baskısı kartları). Ayrıca MiniSectorChip aynı veriyi /dashboard-summary
            # üzerinden zaten alıyor — burada quant_analysis üretmiyoruz.
            return {
                "risk_radar": risk_radar,
                "portfolio_signal": portfolio_signal,
                "vix": vix,  # bonus: frontend Risk Radar modal verdict için
                "last_updated": datetime.now(timezone.utc).isoformat(),
            }

        except Exception as e:
            logger.error(f"Daily digest fatal error: {e}", exc_info=True)
            return {
                "risk_radar": {"title": "AXIOM RISK RADAR", "analysis": "Veri geçici olarak alınamadı.", "symbols": [], "color": "yellow"},
                "portfolio_signal": {"title": "PORTFÖY SINYAL", "recommendation": "Veri geçici olarak alınamadı.", "symbols": [], "color": "blue"},
                "last_updated": datetime.now(timezone.utc).isoformat(),
                "error": str(e),
            }
