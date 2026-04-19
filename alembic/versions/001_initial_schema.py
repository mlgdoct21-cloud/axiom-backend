"""Initial schema migration - SQLite to PostgreSQL

Revision ID: 001
Revises:
Create Date: 2026-04-13 09:30:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '001'
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Users table
    op.create_table(
        'users',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('telegram_id', sa.String(20), nullable=False),
        sa.Column('username', sa.String(32), nullable=True),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default='true'),
        sa.Column('tags', sa.String(500), nullable=False, server_default=''),
        sa.Column('report_mode', sa.String(20), nullable=False, server_default='digest'),
        sa.Column('report_hours', sa.String(100), nullable=False, server_default='08:00'),
        sa.Column('custom_follows', sa.String(1000), nullable=False, server_default=''),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now()),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('telegram_id'),
    )
    op.create_index(op.f('ix_users_telegram_id'), 'users', ['telegram_id'], unique=True)

    # Sources table
    op.create_table(
        'sources',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(255), nullable=False),
        sa.Column('url', sa.String(500), nullable=False),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default='true'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('url'),
    )
    op.create_index(op.f('ix_sources_url'), 'sources', ['url'], unique=True)

    # News items table
    op.create_table(
        'news_items',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('source', sa.String(255), nullable=False),
        sa.Column('original_title', sa.String(500), nullable=False),
        sa.Column('original_link', sa.String(1000), nullable=False),
        sa.Column('ai_summary', sa.Text(), nullable=True),
        sa.Column('ai_tags', sa.String(500), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('original_link'),
    )
    op.create_index(op.f('ix_news_items_source'), 'news_items', ['source'])
    op.create_index(op.f('ix_news_items_original_link'), 'news_items', ['original_link'], unique=True)


def downgrade() -> None:
    op.drop_index(op.f('ix_news_items_original_link'), table_name='news_items')
    op.drop_index(op.f('ix_news_items_source'), table_name='news_items')
    op.drop_table('news_items')

    op.drop_index(op.f('ix_sources_url'), table_name='sources')
    op.drop_table('sources')

    op.drop_index(op.f('ix_users_telegram_id'), table_name='users')
    op.drop_table('users')
