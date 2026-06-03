"""Trade Dossier — trade_dossiers (sembol başına AL/TUT/SAT sentez cache)

Revision ID: 029
Revises: 028
Create Date: 2026-06-03 12:00:00.000000

Lean v3 Faz 3 — kişisel trade aracı. Mehmet bir sembol için "Dossier oluştur"
deyince TA + Temel + Haber + Makro tek bir Gemini 2.5 Flash çağrısı ile
sentezlenir; sonuç payload JSONB olarak buraya yazılır. 30dk cache:
created_at + 30dk taze ise üretmek yerine döndürülür.

Railway alembic auto-run yok; canonical kayıt burada,
core/schema_guard.py runtime garanti veriyor (CREATE IF NOT EXISTS).
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision = '029'
down_revision = '028'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'trade_dossiers',
        sa.Column('id', sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column('symbol', sa.String(length=16), nullable=False),
        sa.Column('symbol_type', sa.String(length=8), nullable=False),
        sa.Column('payload', JSONB, nullable=False),
        sa.Column('model_used', sa.String(length=32), nullable=False, server_default=sa.text("'gemini-2.5-flash'")),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text('NOW()'),
        ),
    )
    op.create_index('ix_trade_dossiers_symbol', 'trade_dossiers', ['symbol'])
    op.create_index('ix_trade_dossiers_created_at', 'trade_dossiers', ['created_at'])


def downgrade() -> None:
    op.drop_index('ix_trade_dossiers_created_at', table_name='trade_dossiers')
    op.drop_index('ix_trade_dossiers_symbol', table_name='trade_dossiers')
    op.drop_table('trade_dossiers')
