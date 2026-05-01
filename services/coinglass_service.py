"""
CoinGlass ETF Flow servisi — real-time spot ETF inflow/outflow veri çekme.

CoinGlass'ın 24h net flow endpoint'i, FMP approximation'ından (volume × change%)
çok daha doğru ETF flow data sağlıyor:
  • FMP: +$16.27M / +211.35 BTC (approximation)
  • CoinGlass: +$23.50M / +310.24 BTC (actual settlement)

API docs: https://www.coinglass.com/api
Hobbyist plan ($29/mo) ETF flow'ı garantili destekliyor.
"""
import os
import asyncio
import aiohttp
from typing import Optional, Dict, Any
from core.logger import get_logger

logger = get_logger("coinglass_service")

COINGLASS_BASE_URL = "https://api.coinglass.com/api/v1"
COINGLASS_TIMEOUT = aiohttp.ClientTimeout(total=8)


def _get_api_key() -> str:
    """Çevre değişkeninden CoinGlass API key'i oku."""
    key = os.getenv("COINGLASS_API_KEY", "").strip()
    return key


async def fetch_etf_flow(
    symbol: str,
    period: str = "24h"
) -> Optional[Dict[str, Any]]:
    """
    CoinGlass API'den ETF flow data çek.

    Args:
        symbol: "BTC" veya "ETH"
        period: "1h", "24h", "7d", "30d" (default: "24h")

    Returns:
        {
            "success": True,
            "data": {
                "net_flow_usd": 23500000,
                "net_flow_coins": 310.24,
                "spot_price": 42500.50,
                "exchanges": [...]
            }
        }
        veya error case'de None.
    """
    api_key = _get_api_key()
    if not api_key:
        logger.error(
            "COINGLASS_API_KEY bulunamadı. "
            ".env'de COINGLASS_API_KEY=<hobbyist-plan-key> ayarla"
        )
        return None

    url = f"{COINGLASS_BASE_URL}/etf_flow"
    headers = {
        "X-API-KEY": api_key,
        "Content-Type": "application/json",
    }
    params = {
        "symbol": symbol,
        "period": period,
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url,
                headers=headers,
                params=params,
                timeout=COINGLASS_TIMEOUT
            ) as resp:
                if resp.status != 200:
                    try:
                        body_preview = (await resp.text())[:200]
                    except Exception:
                        body_preview = "<read-failed>"
                    logger.warning(
                        f"CoinGlass HTTP {resp.status} ({symbol}). "
                        f"Body: {body_preview}"
                    )
                    return None

                data = await resp.json()
                if not isinstance(data, dict):
                    logger.warning(
                        f"CoinGlass beklenmeyen payload: "
                        f"{type(data).__name__}"
                    )
                    return None

                if not data.get("success"):
                    logger.warning(
                        f"CoinGlass API success=false ({symbol}): "
                        f"{data.get('error', 'unknown error')}"
                    )
                    return None

                logger.info(
                    f"✓ CoinGlass[{symbol}]: "
                    f"net_flow_usd={data.get('data', {}).get('net_flow_usd', 0)}"
                )
                return data

    except asyncio.TimeoutError:
        logger.warning(
            f"CoinGlass timeout (8s) — {symbol} atlandı"
        )
        return None

    except Exception as e:
        logger.warning(f"CoinGlass hatası ({symbol}): {e}")
        return None
