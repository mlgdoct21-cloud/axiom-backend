"""BIST TL UFRS Bilanço Scraper — isyatirimhisse adaptörü.

İş Yatırım'ın public API'sini (`https://www.isyatirim.com.tr/...`) kullanan
`isyatirimhisse` PyPI paketi üzerinden BIST hisselerinin TL bazlı bilanço,
gelir tablosu ve nakit akış verisini çeker; long-format `bist_financials`
tablosuna UPSERT eder (her satır = symbol+period+item_code).

Karar gerekçesi:
- FMP Türk hisselerinde TL UFRS tablo döndürmüyor → AXIOM Temel Analiz sekmesi
  BIST sembollerinde boş kalıyor.
- İş Yatırım public scraper (MIT lisans) ücretsiz, stabil API, kuruma
  doğrudan bağlanıyor (aracı kontratı/üyelik gerekmez).
- Veri çeyreklik (Q1/Q2/Q3/Q4) güncellenir → haftalık cron yeterli
  (KAP bildirim akışıyla anlık güncelleme Faz 2/3'te).

Sınırlamalar (load-bearing):
- HTTP scraping → IP-ban riski; tek sefer max 1 sembol, 5sn delay.
- Sadece BIST_SYMBOLS env listesindeki ~20 sembol (BIST 30'un en likitleri).
- isyatirimhisse pandas DataFrame döndürür → asyncio thread-pool'da çağrılır.
"""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from typing import List, Optional

from sqlalchemy import text

from core.database import engine
from core.logger import get_logger
from services.market_detector import BIST_SYMBOLS

logger = get_logger("bist_financials")

# Haftalık cron aralığı (saniye). Default = 7 gün.
BIST_FINANCIALS_INTERVAL_SEC = int(os.getenv("BIST_FINANCIALS_INTERVAL_SEC", str(7 * 24 * 3600)))
# Her sembol arası bekleme — İş Yatırım'ı yormamak ve IP-ban'dan kaçınmak için.
BIST_FINANCIALS_DELAY_SEC = int(os.getenv("BIST_FINANCIALS_DELAY_SEC", "5"))
# Geri dönülecek yıl sayısı (current_year - LOOKBACK_YEARS .. current_year).
BIST_FINANCIALS_LOOKBACK_YEARS = int(os.getenv("BIST_FINANCIALS_LOOKBACK_YEARS", "3"))
# Hangi UFRS grubu? '1' = XI_29 (eski sermaye piyasası), '2' = UFRS, '3' = UFRS_K.
BIST_FINANCIALS_GROUP = os.getenv("BIST_FINANCIALS_GROUP", "1")


def _fetch_one_sync(symbol: str, start_year: int, end_year: int, group: str):
    """Sync wrapper for isyatirimhisse.fetch_financials.

    Threadpool'da çağrılır; isyatirimhisse synchronous requests kullanıyor.
    Hata durumunda None döner — tek sembol kırılırsa diğerleri devam.
    """
    try:
        # Import burada — paket Railway image'ında varsa runtime'da yüklenir.
        from isyatirimhisse import fetch_financials
    except ImportError as e:
        logger.error(f"isyatirimhisse paketi yüklenmemiş: {e}")
        return None

    try:
        df = fetch_financials(
            symbols=[symbol],
            start_year=start_year,
            end_year=end_year,
            exchange="TRY",
            financial_group=group,
        )
        if df is None or df.empty:
            return None
        return df
    except Exception as e:
        logger.warning(f"BIST[{symbol}] fetch_financials hatası: {e}")
        return None


def _df_to_rows(df, symbol: str, group: str) -> List[dict]:
    """Pivot DataFrame'i long-format dict satırlarına çevirir.

    Input columns (isyatirimhisse v5.0.1):
      FINANCIAL_ITEM_CODE, FINANCIAL_ITEM_NAME_TR, FINANCIAL_ITEM_NAME_EN,
      'YYYY/Q' (1..N adet), SYMBOL.

    Output: her item × her period için tek satır.
    """
    rows: List[dict] = []
    base_cols = {"FINANCIAL_ITEM_CODE", "FINANCIAL_ITEM_NAME_TR", "FINANCIAL_ITEM_NAME_EN", "SYMBOL"}
    period_cols = [c for c in df.columns if c not in base_cols]

    for _, r in df.iterrows():
        code = str(r.get("FINANCIAL_ITEM_CODE") or "").strip()
        if not code:
            continue
        name_tr = str(r.get("FINANCIAL_ITEM_NAME_TR") or "").strip() or None
        name_en = str(r.get("FINANCIAL_ITEM_NAME_EN") or "").strip() or None
        for period in period_cols:
            v = r.get(period)
            # pandas NaN → None
            try:
                if v is None:
                    val = None
                else:
                    fv = float(v)
                    val = None if (fv != fv) else fv  # NaN check
            except (TypeError, ValueError):
                val = None
            rows.append({
                "symbol": symbol.upper(),
                "period": str(period),
                "item_code": code,
                "item_name_tr": name_tr,
                "item_name_en": name_en,
                "value": val,
                "currency": "TRY",
                "financial_group": group,
                "source": "isyatirimhisse",
            })
    return rows


async def _upsert_rows(rows: List[dict]) -> int:
    """Batch UPSERT — PostgreSQL ON CONFLICT, SQLite INSERT OR REPLACE.

    Returns: yazılan satır sayısı.
    """
    if not rows:
        return 0
    dialect = engine.dialect.name
    if dialect == "postgresql":
        sql = text("""
            INSERT INTO bist_financials
              (symbol, period, item_code, item_name_tr, item_name_en, value,
               currency, financial_group, source, fetched_at)
            VALUES
              (:symbol, :period, :item_code, :item_name_tr, :item_name_en, :value,
               :currency, :financial_group, :source, NOW())
            ON CONFLICT (symbol, period, item_code, currency, financial_group)
            DO UPDATE SET
              item_name_tr = EXCLUDED.item_name_tr,
              item_name_en = EXCLUDED.item_name_en,
              value = EXCLUDED.value,
              fetched_at = NOW()
        """)
    else:
        # SQLite fallback (lokal dev). UNIQUE constraint REPLACE'a yetmiyor —
        # tablo schema'da unique var, INSERT OR REPLACE çalışır.
        sql = text("""
            INSERT OR REPLACE INTO bist_financials
              (symbol, period, item_code, item_name_tr, item_name_en, value,
               currency, financial_group, source, fetched_at)
            VALUES
              (:symbol, :period, :item_code, :item_name_tr, :item_name_en, :value,
               :currency, :financial_group, :source, CURRENT_TIMESTAMP)
        """)
    async with engine.begin() as conn:
        await conn.execute(sql, rows)
    return len(rows)


async def refresh_one_symbol(symbol: str, group: Optional[str] = None) -> int:
    """Tek BIST sembolünün TL bilanço/gelir/nakit verisini scrape eder + UPSERT.

    Returns: DB'ye yazılan satır sayısı (period × item kombinasyonu).
    """
    group = group or BIST_FINANCIALS_GROUP
    current_year = datetime.now(timezone.utc).year
    start_year = current_year - BIST_FINANCIALS_LOOKBACK_YEARS

    df = await asyncio.to_thread(_fetch_one_sync, symbol, start_year, current_year, group)
    if df is None:
        return 0

    rows = _df_to_rows(df, symbol, group)
    if not rows:
        return 0

    try:
        written = await _upsert_rows(rows)
        logger.info(f"📊 BIST[{symbol}] {written} satır yazıldı ({len(df.columns) - 4} period)")
        return written
    except Exception as e:
        logger.error(f"BIST[{symbol}] UPSERT hatası: {e}")
        return 0


async def refresh_all_symbols(symbols: Optional[List[str]] = None) -> dict:
    """BIST_SYMBOLS listesindeki tüm sembolleri sırayla yeniler (delay'li)."""
    targets = symbols or BIST_SYMBOLS
    total_rows = 0
    succeeded = 0
    failed: List[str] = []

    logger.info(f"🇹🇷 BIST financials cron başladı — {len(targets)} sembol")
    for sym in targets:
        try:
            n = await refresh_one_symbol(sym)
            if n > 0:
                total_rows += n
                succeeded += 1
            else:
                failed.append(sym)
        except Exception as e:
            logger.error(f"BIST[{sym}] beklenmeyen hata: {e}")
            failed.append(sym)
        await asyncio.sleep(BIST_FINANCIALS_DELAY_SEC)

    logger.info(
        f"🇹🇷 BIST financials cron bitti — {succeeded}/{len(targets)} sembol, "
        f"{total_rows} satır, başarısız: {failed or '—'}"
    )
    return {
        "succeeded": succeeded,
        "total_symbols": len(targets),
        "total_rows": total_rows,
        "failed": failed,
    }


async def bist_financials_loop() -> None:
    """Haftalık supervisor — `main.py` lifespan'inde başlatılır.

    İlk çalıştırma 5 dakika sonra (startup race'i önlemek için), sonra
    BIST_FINANCIALS_INTERVAL_SEC (default 7 gün) cadence.
    Hata durumunda 30dk backoff + tekrar dene.
    """
    logger.info(f"🟢 BIST financials loop başlatıldı (her {BIST_FINANCIALS_INTERVAL_SEC}s)")
    # İlk run startup'tan 5dk sonra
    await asyncio.sleep(300)
    while True:
        try:
            await refresh_all_symbols()
            await asyncio.sleep(BIST_FINANCIALS_INTERVAL_SEC)
        except asyncio.CancelledError:
            logger.info("BIST financials loop durduruldu (cancel).")
            break
        except Exception as e:
            logger.error(f"BIST financials loop çöktü: {e}", exc_info=True)
            await asyncio.sleep(1800)  # 30dk backoff


async def get_symbol_financials(symbol: str, group: Optional[str] = None) -> dict:
    """API endpoint için: tek sembolün son N period'unu çek.

    Returns:
      {
        "symbol": "ASELS",
        "currency": "TRY",
        "periods": ["2024/3", "2024/6", ...],
        "items": [
            {"code": "1A", "name_tr": "Dönen Varlıklar", "values": {"2024/3": 7.4e10, ...}},
            ...
        ],
        "fetched_at": "2026-06-01T12:00:00+00:00"
      }
    """
    sym = symbol.upper()
    group = group or BIST_FINANCIALS_GROUP
    sql = text("""
        SELECT period, item_code, item_name_tr, item_name_en, value, fetched_at
        FROM bist_financials
        WHERE symbol = :sym AND currency = 'TRY' AND financial_group = :grp
        ORDER BY item_code, period
    """)
    async with engine.begin() as conn:
        result = await conn.execute(sql, {"sym": sym, "grp": group})
        rows = result.fetchall()

    if not rows:
        return {"symbol": sym, "currency": "TRY", "periods": [], "items": [], "fetched_at": None}

    periods_set: set = set()
    items_map: dict = {}
    latest_fetch: Optional[datetime] = None
    for r in rows:
        period = r[0]
        code = r[1]
        name_tr = r[2]
        name_en = r[3]
        value = r[4]
        fetched = r[5]
        periods_set.add(period)
        if code not in items_map:
            items_map[code] = {
                "code": code,
                "name_tr": name_tr,
                "name_en": name_en,
                "values": {},
            }
        # Decimal → float (frontend JSON serializable)
        items_map[code]["values"][period] = float(value) if value is not None else None
        if fetched is not None and (latest_fetch is None or fetched > latest_fetch):
            latest_fetch = fetched

    periods = sorted(periods_set)  # "2024/12" < "2025/3" çünkü zero-pad değil; manuel sort:
    def _period_key(p: str) -> tuple:
        try:
            y, q = p.split("/")
            return (int(y), int(q))
        except Exception:
            return (0, 0)
    periods = sorted(periods_set, key=_period_key)

    return {
        "symbol": sym,
        "currency": "TRY",
        "financial_group": group,
        "periods": periods,
        "items": list(items_map.values()),
        "fetched_at": latest_fetch.isoformat() if latest_fetch else None,
    }
