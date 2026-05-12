"""Add macro_pre_announcements table — T-5dk pre-warm broadcast idempotency.

Revision ID: 023
Revises: 022
Create Date: 2026-05-12 18:00:00.000000

Kullanıcı tarafında "biz piyasayı izliyoruz" engagement için, major release
event'lerden 5 dakika önce tüm kullanıcılara pre-announce mesaj gönderilir.
Idempotency için event_key (event_type + scheduled_at.date) deterministik PK.
"""
from alembic import op
import sqlalchemy as sa


revision = "023"
down_revision = "022"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "macro_pre_announcements",
        sa.Column("event_key", sa.String(length=128), primary_key=True),
        sa.Column("event_type", sa.String(length=32), nullable=False),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("recipients_count", sa.Integer, nullable=False, server_default="0"),
    )


def downgrade():
    op.drop_table("macro_pre_announcements")
