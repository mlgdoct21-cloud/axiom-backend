"""Opsiyon Akademisi Faz 1 — user_academy_progress

Revision ID: 027
Revises: 026
Create Date: 2026-05-20 12:00:00.000000

Statik müfredat (data/academy/{curriculum,glossary}.yaml) DB'ye girmez;
sadece kullanıcı ilerlemesi (lesson tamamlama + quiz skor) bu tablodadır.

Railway alembic auto-run yok; core/schema_guard.py runtime garanti.
"""
from alembic import op
import sqlalchemy as sa


revision = '027'
down_revision = '026'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'user_academy_progress',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('lesson_id', sa.String(length=16), nullable=False),
        sa.Column('quiz_score', sa.Integer(), nullable=True),
        sa.Column(
            'attempts',
            sa.Integer(),
            nullable=False,
            server_default=sa.text('1'),
        ),
        sa.Column(
            'completed_at',
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text('NOW()'),
        ),
        sa.Column(
            'updated_at',
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text('NOW()'),
        ),
        sa.UniqueConstraint('user_id', 'lesson_id', name='uq_acad_user_lesson'),
    )
    op.create_index('ix_acad_user', 'user_academy_progress', ['user_id'])


def downgrade() -> None:
    op.drop_index('ix_acad_user', table_name='user_academy_progress')
    op.drop_table('user_academy_progress')
