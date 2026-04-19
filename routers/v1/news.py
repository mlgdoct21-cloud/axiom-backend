"""News API routes - Retrieval and Filtering"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from typing import List

from core.database import get_db
from core.security import get_current_user, get_current_user_id
from schemas.news_schema import NewsResponse, NewsFilter
from schemas.error_schema import ErrorResponse
from services.news import NewsService
from core.logger import get_logger
from models.user import User

logger = get_logger("news_router")

router = APIRouter(
    prefix="/news",
    tags=["news"],
)


@router.get(
    "",
    response_model=List[NewsResponse],
    status_code=status.HTTP_200_OK,
    responses={
        401: {"model": ErrorResponse, "description": "Unauthorized"},
        500: {"model": ErrorResponse, "description": "Server error"}
    }
)
async def get_news(
    skip: int = 0,
    limit: int = 50,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """
    Get all news with pagination (newest first)

    - **skip**: Number of items to skip (default 0)
    - **limit**: Maximum items to return (default 50, max 100)
    """
    try:
        # Limit maximum results
        if limit > 100:
            limit = 100

        news = await NewsService.get_all_news(db, skip=skip, limit=limit)
        return [NewsResponse.from_orm(n) for n in news]
    except Exception as e:
        logger.error(f"Error getting news: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to get news"
        )


@router.get(
    "/latest",
    response_model=List[NewsResponse],
    status_code=status.HTTP_200_OK,
    responses={
        401: {"model": ErrorResponse, "description": "Unauthorized"},
        500: {"model": ErrorResponse, "description": "Server error"}
    }
)
async def get_latest_news(
    limit: int = 10,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Get latest news items"""
    try:
        if limit > 50:
            limit = 50

        news = await NewsService.get_latest_news(db, limit=limit)
        return [NewsResponse.from_orm(n) for n in news]
    except Exception as e:
        logger.error(f"Error getting latest news: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to get latest news"
        )


@router.get(
    "/search",
    response_model=List[NewsResponse],
    status_code=status.HTTP_200_OK,
    responses={
        401: {"model": ErrorResponse, "description": "Unauthorized"},
        400: {"model": ErrorResponse, "description": "Invalid search query"},
        500: {"model": ErrorResponse, "description": "Server error"}
    }
)
async def search_news(
    q: str,
    skip: int = 0,
    limit: int = 50,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """
    Search news by title, summary, or tags

    - **q**: Search query (required, min 2 characters)
    - **skip**: Number of items to skip
    - **limit**: Maximum items to return
    """
    try:
        if not q or len(q) < 2:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Search query must be at least 2 characters"
            )

        if limit > 100:
            limit = 100

        news = await NewsService.search_news(db, q, skip=skip, limit=limit)
        return [NewsResponse.from_orm(n) for n in news]
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error searching news: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to search news"
        )


@router.get(
    "/source/{source}",
    response_model=List[NewsResponse],
    status_code=status.HTTP_200_OK,
    responses={
        401: {"model": ErrorResponse, "description": "Unauthorized"},
        500: {"model": ErrorResponse, "description": "Server error"}
    }
)
async def get_news_by_source(
    source: str,
    skip: int = 0,
    limit: int = 50,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """
    Get news from a specific source

    - **source**: Source name (e.g., "Bloomberg", "Yahoo Finance")
    - **skip**: Number of items to skip
    - **limit**: Maximum items to return
    """
    try:
        if limit > 100:
            limit = 100

        news = await NewsService.get_news_by_source(db, source, skip=skip, limit=limit)
        return [NewsResponse.from_orm(n) for n in news]
    except Exception as e:
        logger.error(f"Error getting news by source: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to get news by source"
        )


@router.get(
    "/tag/{tag}",
    response_model=List[NewsResponse],
    status_code=status.HTTP_200_OK,
    responses={
        401: {"model": ErrorResponse, "description": "Unauthorized"},
        500: {"model": ErrorResponse, "description": "Server error"}
    }
)
async def get_news_by_tag(
    tag: str,
    skip: int = 0,
    limit: int = 50,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """
    Get news by tag (AI-generated tags)

    - **tag**: Tag to search for
    - **skip**: Number of items to skip
    - **limit**: Maximum items to return
    """
    try:
        if not tag or len(tag) < 2:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Tag must be at least 2 characters"
            )

        if limit > 100:
            limit = 100

        news = await NewsService.get_news_by_tag(db, tag, skip=skip, limit=limit)
        return [NewsResponse.from_orm(n) for n in news]
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting news by tag: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to get news by tag"
        )


@router.post(
    "/filter",
    response_model=List[NewsResponse],
    status_code=status.HTTP_200_OK,
    responses={
        401: {"model": ErrorResponse, "description": "Unauthorized"},
        500: {"model": ErrorResponse, "description": "Server error"}
    }
)
async def filter_news(
    filters: NewsFilter,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """
    Advanced news filtering

    Request body:
    - **source**: Filter by source (optional)
    - **tag**: Filter by AI tag (optional)
    - **search**: Full-text search (optional)
    - **skip**: Pagination offset
    - **limit**: Maximum results
    """
    try:
        # Validate limit
        if filters.limit > 100:
            filters.limit = 100

        news = await NewsService.filter_news(db, filters)
        return [NewsResponse.from_orm(n) for n in news]
    except Exception as e:
        logger.error(f"Error filtering news: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to filter news"
        )


@router.get(
    "/{news_id}",
    response_model=NewsResponse,
    status_code=status.HTTP_200_OK,
    responses={
        401: {"model": ErrorResponse, "description": "Unauthorized"},
        404: {"model": ErrorResponse, "description": "News not found"},
        500: {"model": ErrorResponse, "description": "Server error"}
    }
)
async def get_news_by_id(
    news_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Get specific news item by ID"""
    try:
        news = await NewsService.get_news_by_id(db, news_id)

        if not news:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="News item not found"
            )

        return NewsResponse.from_orm(news)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting news by ID: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to get news item"
        )
