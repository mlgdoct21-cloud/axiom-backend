"""Technical Analysis Routes - Charts, Indicators, Signals"""

from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy.ext.asyncio import AsyncSession
from typing import List, Optional

from core.database import get_db
from core.security import get_current_user
from models.user import User
from services.price_service import PriceService
from services.indicator_service import IndicatorService
from services.signal_service import SignalService
from schemas.error_schema import ErrorResponse
from core.logger import get_logger

logger = get_logger("technical_router")

router = APIRouter(
    prefix="/technical",
    tags=["technical-analysis"],
)


@router.get(
    "/price/{symbol}",
    status_code=status.HTTP_200_OK,
    responses={
        401: {"model": ErrorResponse, "description": "Unauthorized"},
        400: {"model": ErrorResponse, "description": "Invalid symbol"},
        500: {"model": ErrorResponse, "description": "Server error"}
    }
)
async def get_price(
    symbol: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """
    Get latest price for a symbol

    - **symbol**: Trading symbol (e.g., BTC, ETH, AAPL)
    """
    try:
        # Validate symbol
        if not await PriceService.validate_symbol(symbol):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Symbol {symbol} not supported"
            )

        price_data = await PriceService.fetch_latest_price(symbol.upper())
        if not price_data:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Could not fetch price for {symbol}"
            )

        return price_data

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting price for {symbol}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to get price"
        )


@router.get(
    "/chart/{symbol}",
    status_code=status.HTTP_200_OK,
    responses={
        401: {"model": ErrorResponse, "description": "Unauthorized"},
        400: {"model": ErrorResponse, "description": "Invalid symbol"},
        500: {"model": ErrorResponse, "description": "Server error"}
    }
)
async def get_chart(
    symbol: str,
    period: str = Query("1d", regex="^(1m|5m|15m|30m|1h|4h|1d|1w|1mo)$"),
    limit: int = Query(100, ge=10, le=500),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """
    Get OHLCV chart data for a symbol

    - **symbol**: Trading symbol (e.g., BTC, ETH, AAPL)
    - **period**: Timeframe (1m, 5m, 15m, 30m, 1h, 4h, 1d, 1w, 1mo)
    - **limit**: Number of candles (10-500, default 100)
    """
    try:
        # Validate symbol
        if not await PriceService.validate_symbol(symbol):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Symbol {symbol} not supported"
            )

        ohlcv_list = await PriceService.fetch_ohlcv(symbol.upper(), period, limit)
        if not ohlcv_list:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Could not fetch chart data for {symbol}"
            )

        return [ohlcv.to_dict() for ohlcv in ohlcv_list]

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting chart for {symbol}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to get chart data"
        )


@router.get(
    "/indicators/{symbol}",
    status_code=status.HTTP_200_OK,
    responses={
        401: {"model": ErrorResponse, "description": "Unauthorized"},
        400: {"model": ErrorResponse, "description": "Invalid symbol"},
        500: {"model": ErrorResponse, "description": "Server error"}
    }
)
async def get_indicators(
    symbol: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """
    Get technical indicators for a symbol

    Includes: RSI, MACD, Bollinger Bands, SMA

    - **symbol**: Trading symbol (e.g., BTC, ETH, AAPL)
    """
    try:
        # Validate symbol
        if not await PriceService.validate_symbol(symbol):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Symbol {symbol} not supported"
            )

        # Fetch price data
        ohlcv_list = await PriceService.fetch_ohlcv(symbol.upper(), period="1d", limit=100)
        if not ohlcv_list:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Could not fetch data for {symbol}"
            )

        prices = [ohlcv.close for ohlcv in ohlcv_list]

        # Calculate indicators
        indicators = IndicatorService.calculate_all_indicators(prices)
        if not indicators:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Could not calculate indicators for {symbol}"
            )

        return indicators

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting indicators for {symbol}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to get indicators"
        )


@router.get(
    "/signals/{symbol}",
    status_code=status.HTTP_200_OK,
    responses={
        401: {"model": ErrorResponse, "description": "Unauthorized"},
        400: {"model": ErrorResponse, "description": "Invalid symbol"},
        500: {"model": ErrorResponse, "description": "Server error"}
    }
)
async def get_signal(
    symbol: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """
    Get trading signal for a symbol

    Returns: BUY, SELL, or HOLD signal with confidence and reasoning

    - **symbol**: Trading symbol (e.g., BTC, ETH, AAPL)
    """
    try:
        # Validate symbol
        if not await PriceService.validate_symbol(symbol):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Symbol {symbol} not supported"
            )

        signal = await SignalService.generate_signal(symbol.upper())
        if not signal:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Could not generate signal for {symbol}"
            )

        return signal

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting signal for {symbol}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to generate signal"
        )


@router.get(
    "/signals",
    status_code=status.HTTP_200_OK,
    responses={
        401: {"model": ErrorResponse, "description": "Unauthorized"},
        500: {"model": ErrorResponse, "description": "Server error"}
    }
)
async def get_all_signals(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """
    Get trading signals for all supported symbols

    Returns: List of signals with confidence and reasoning
    """
    try:
        symbols = PriceService.get_supported_symbols()
        signals = await SignalService.generate_signals_batch(symbols)

        if not signals:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Could not generate signals"
            )

        return {
            "symbols_count": len(signals),
            "signals": signals
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting all signals: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to generate signals"
        )


@router.get(
    "/supported-symbols",
    status_code=status.HTTP_200_OK,
)
async def get_supported_symbols(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """
    Get list of supported trading symbols
    """
    symbols = PriceService.get_supported_symbols()
    return {
        "symbols": symbols,
        "count": len(symbols)
    }
