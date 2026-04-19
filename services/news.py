"""News service - Retrieval and filtering operations"""

from typing import Optional, List
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_, or_
from models.news import NewsItem
from schemas.news_schema import NewsCreate, NewsResponse, NewsFilter
from core.logger import get_logger

logger = get_logger("news_service")


class NewsService:
    """News management service"""

    @staticmethod
    async def create_news(db: AsyncSession, news_data: NewsCreate) -> NewsResponse:
        """Create a new news item"""
        try:
            # Check if news already exists (by link)
            existing = await NewsService.get_news_by_link(db, news_data.original_link)
            if existing:
                logger.debug(f"News item with link {news_data.original_link} already exists")
                return NewsResponse.from_orm(existing)

            # Create new news item
            db_news = NewsItem(
                source=news_data.source,
                original_title=news_data.original_title,
                original_link=news_data.original_link,
                ai_summary=news_data.ai_summary,
                ai_tags=news_data.ai_tags
            )
            db.add(db_news)
            await db.commit()
            await db.refresh(db_news)
            logger.info(f"News item created: {news_data.source} - {news_data.original_title[:50]}")
            return NewsResponse.from_orm(db_news)
        except Exception as e:
            logger.error(f"Error creating news item: {e}")
            await db.rollback()
            raise

    @staticmethod
    async def get_news_by_id(db: AsyncSession, news_id: int) -> Optional[NewsItem]:
        """Get news by ID"""
        try:
            result = await db.execute(select(NewsItem).filter(NewsItem.id == news_id))
            return result.scalar_one_or_none()
        except Exception as e:
            logger.error(f"Error getting news by ID {news_id}: {e}")
            return None

    @staticmethod
    async def get_news_by_link(db: AsyncSession, link: str) -> Optional[NewsItem]:
        """Get news by original link"""
        try:
            result = await db.execute(
                select(NewsItem).filter(NewsItem.original_link == link)
            )
            return result.scalar_one_or_none()
        except Exception as e:
            logger.error(f"Error getting news by link: {e}")
            return None

    @staticmethod
    async def get_all_news(
        db: AsyncSession,
        skip: int = 0,
        limit: int = 50
    ) -> List[NewsItem]:
        """Get all news with pagination (newest first)"""
        try:
            result = await db.execute(
                select(NewsItem)
                .order_by(NewsItem.created_at.desc())
                .offset(skip)
                .limit(limit)
            )
            return result.scalars().all()
        except Exception as e:
            logger.error(f"Error getting all news: {e}")
            return []

    @staticmethod
    async def get_news_by_source(
        db: AsyncSession,
        source: str,
        skip: int = 0,
        limit: int = 50
    ) -> List[NewsItem]:
        """Get news by source"""
        try:
            result = await db.execute(
                select(NewsItem)
                .filter(NewsItem.source == source)
                .order_by(NewsItem.created_at.desc())
                .offset(skip)
                .limit(limit)
            )
            return result.scalars().all()
        except Exception as e:
            logger.error(f"Error getting news by source {source}: {e}")
            return []

    @staticmethod
    async def get_news_by_tag(
        db: AsyncSession,
        tag: str,
        skip: int = 0,
        limit: int = 50
    ) -> List[NewsItem]:
        """Get news by tag (searches ai_tags field)"""
        try:
            result = await db.execute(
                select(NewsItem)
                .filter(NewsItem.ai_tags.contains(tag))
                .order_by(NewsItem.created_at.desc())
                .offset(skip)
                .limit(limit)
            )
            return result.scalars().all()
        except Exception as e:
            logger.error(f"Error getting news by tag {tag}: {e}")
            return []

    @staticmethod
    async def search_news(
        db: AsyncSession,
        search_query: str,
        skip: int = 0,
        limit: int = 50
    ) -> List[NewsItem]:
        """Search news by title, summary, or tags"""
        try:
            search_pattern = f"%{search_query}%"
            result = await db.execute(
                select(NewsItem)
                .filter(
                    or_(
                        NewsItem.original_title.ilike(search_pattern),
                        NewsItem.ai_summary.ilike(search_pattern),
                        NewsItem.ai_tags.ilike(search_pattern)
                    )
                )
                .order_by(NewsItem.created_at.desc())
                .offset(skip)
                .limit(limit)
            )
            return result.scalars().all()
        except Exception as e:
            logger.error(f"Error searching news: {e}")
            return []

    @staticmethod
    async def filter_news(
        db: AsyncSession,
        filters: NewsFilter
    ) -> List[NewsItem]:
        """Filter news with multiple conditions"""
        try:
            query = select(NewsItem)

            # Add source filter
            if filters.source:
                query = query.filter(NewsItem.source == filters.source)

            # Add tag filter
            if filters.tag:
                query = query.filter(NewsItem.ai_tags.contains(filters.tag))

            # Add search filter
            if filters.search:
                search_pattern = f"%{filters.search}%"
                query = query.filter(
                    or_(
                        NewsItem.original_title.ilike(search_pattern),
                        NewsItem.ai_summary.ilike(search_pattern),
                        NewsItem.ai_tags.ilike(search_pattern)
                    )
                )

            # Order by created_at DESC and apply pagination
            query = query.order_by(NewsItem.created_at.desc())
            query = query.offset(filters.skip).limit(filters.limit)

            result = await db.execute(query)
            return result.scalars().all()
        except Exception as e:
            logger.error(f"Error filtering news: {e}")
            return []

    @staticmethod
    async def get_news_count(db: AsyncSession) -> int:
        """Get total count of news items"""
        try:
            result = await db.execute(select(NewsItem))
            return len(result.scalars().all())
        except Exception as e:
            logger.error(f"Error getting news count: {e}")
            return 0

    @staticmethod
    async def delete_old_news(db: AsyncSession, days: int = 30) -> int:
        """Delete news older than specified days (maintenance operation)"""
        try:
            from datetime import datetime, timedelta
            from sqlalchemy import delete

            cutoff_date = datetime.utcnow() - timedelta(days=days)
            stmt = delete(NewsItem).where(NewsItem.created_at < cutoff_date)
            result = await db.execute(stmt)
            await db.commit()
            logger.info(f"Deleted {result.rowcount} old news items")
            return result.rowcount
        except Exception as e:
            logger.error(f"Error deleting old news: {e}")
            await db.rollback()
            return 0

    @staticmethod
    async def get_latest_news(db: AsyncSession, limit: int = 10) -> List[NewsItem]:
        """Get latest news items"""
        try:
            result = await db.execute(
                select(NewsItem)
                .order_by(NewsItem.created_at.desc())
                .limit(limit)
            )
            return result.scalars().all()
        except Exception as e:
            logger.error(f"Error getting latest news: {e}")
            return []
