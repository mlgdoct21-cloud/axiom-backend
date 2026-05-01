"""
Seed ETF flow cache with manual CoinGlass values.

CoinGlass /etf/bitcoin and /etf/ethereum (read 2026-05-01) — Apr 30 daily flow
data. Bitbo publishes Apr N data on Apr N+1 with ~24h lag, so we manually push
the freshest CoinGlass cell into cache.

Run on Railway:
    railway run python scripts/seed_etf_cache.py
"""
import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


CG_VALUES = {
    "BTC": {
        "net_flow_usd": 23_500_000.0,
        "net_flow_coins": 310.24,
        "spot_price": 75_745.0,  # implied from $23.5M / 310.24 BTC
        "total_aum_usd": 101_808_390_420.0,
        "total_holdings_coins": 739_264.68,
    },
    "ETH": {
        "net_flow_usd": -24_210_214.0,  # -10,526.18 ETH × ~$2,300
        "net_flow_coins": -10_526.18,
        "spot_price": 2_300.0,
        "total_aum_usd": 13_694_639_736.0,
        "total_holdings_coins": 3_146_441.87,
    },
}


async def main():
    from services.etf_flow_cache_service import save_etf_flow

    for symbol, vals in CG_VALUES.items():
        new_id = await save_etf_flow(
            symbol=symbol,
            net_flow_usd=vals["net_flow_usd"],
            net_flow_coins=vals["net_flow_coins"],
            spot_price=vals["spot_price"],
            total_aum_usd=vals["total_aum_usd"],
            total_holdings_coins=vals["total_holdings_coins"],
            source="coinglass_manual",
            raw_data={"date": "2026-04-30", "scraped_via": "claude-in-chrome"},
        )
        print(f"  ✓ {symbol}: id={new_id}, ${vals['net_flow_usd']:,.0f} / "
              f"{vals['net_flow_coins']:,.2f}")


if __name__ == "__main__":
    asyncio.run(main())
