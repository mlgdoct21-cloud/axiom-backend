"""Add broadcasted_at columns to macro_stories for tier-gated Telegram push idempotency

Revision ID: 018
Revises: 017
Create Date: 2026-05-11 12:00:00.000000

Premium/Advance Telegram broadcast loop tamamlanıyor: hikayeler yazılınca
fire-and-forget background task `broadcast_story(event_id, tier)` çalışacak.
Idempotency için satır başına `broadcasted_premium_at` ve `broadcasted_advance_at`
TIMESTAMP'leri tutuyoruz — Day 28'in `last_broadcast_at` pattern'ine paralel.

Aynı satırda 2 kolon çünkü `macro_stories` (event_id, tier) UNIQUE; tier
ayrı satırlarda ama biz "hangi tier kullanıcısına push attık" durumunu
satırı yazan tier hikayesinde takip ediyoruz: Premium hikayesi yazıldı +
push'landıysa o satırın `broadcasted_premium_at` doludur.
"""
from alembic import op
import sqlalchemy as sa


revision = '018'
down_revision = '017'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'macro_stories',
        sa.Column('broadcasted_premium_at', sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        'macro_stories',
        sa.Column('broadcasted_advance_at', sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('macro_stories', 'broadcasted_advance_at')
    op.drop_column('macro_stories', 'broadcasted_premium_at')
