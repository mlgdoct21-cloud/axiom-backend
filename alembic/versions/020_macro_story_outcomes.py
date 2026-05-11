"""Add macro_story_outcomes table — track record / İsabet Skorboard.

Revision ID: 020
Revises: 019
Create Date: 2026-05-11 15:00:00.000000

FAZ D — verdict-vs-gerçekleşme skorboard. Storyteller her hikayede "Senin
için 1-cümle" bölümünde YÖN tahmin verir (BTC pozitif/baskı, USD güçlü/zayıf,
faiz patika hawkish/dovish). Bu tahmini extract edip sonraki release ile
KARŞILAŞTIRIYORUZ.

Şema:
- story_event_id + tier: hangi hikaye (macro_stories'a FK)
- predicted_verdict: 'bullish_btc' | 'bearish_btc' | 'hawkish_fed' | 'dovish_fed' | ...
- predicted_at: tahminin yapıldığı zaman
- horizon_days: tahminin hangi vadeye bakıldığı (örn 30 = next CPI)
- actual_outcome: gerçekleşen (JSON: {compared_event_id, delta, outcome_label})
- hit_score: -1 (kaçırdı) / 0 (kararsız) / +1 (isabet)
- validated_at + validated_by: manuel admin onayı (auto-detect başlangıçta off)

Default FEATURE FLAG OFF — kullanıcı önce 3-5 hikayeyi manuel valide edecek,
hit_rate >%50 çıkarsa MACRO_TRACK_RECORD_ENABLED=true ile aç.
"""
from alembic import op
import sqlalchemy as sa


revision = '020'
down_revision = '019'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'macro_story_outcomes',
        sa.Column('id', sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column('story_event_id', sa.Text(), nullable=False),
        sa.Column('tier', sa.Text(), nullable=False),  # premium | advance
        sa.Column('event_type', sa.Text(), nullable=False),
        sa.Column('predicted_verdict', sa.Text(), nullable=False),
        sa.Column('predicted_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('horizon_days', sa.Integer(), nullable=False, server_default=sa.text('30')),
        sa.Column('compared_event_id', sa.Text(), nullable=True),
        sa.Column('actual_outcome', sa.dialects.postgresql.JSONB(), nullable=True),
        sa.Column('hit_score', sa.Numeric(3, 2), nullable=True),  # -1, 0, +1 (or fractional)
        sa.Column('auto_inferred', sa.Boolean(), nullable=False, server_default=sa.text('FALSE')),
        sa.Column('validated_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('validated_by', sa.Text(), nullable=True),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text('NOW()'),
        ),
    )
    op.create_index(
        'ix_story_outcomes_event_type_tier',
        'macro_story_outcomes',
        ['event_type', 'tier'],
    )
    op.create_index(
        'ix_story_outcomes_story_event_id',
        'macro_story_outcomes',
        ['story_event_id'],
    )
    # Unique constraint: bir (story_event_id, tier) çifti için tek outcome row.
    op.create_unique_constraint(
        'uq_story_outcomes_story_tier',
        'macro_story_outcomes',
        ['story_event_id', 'tier'],
    )


def downgrade() -> None:
    op.drop_constraint('uq_story_outcomes_story_tier', 'macro_story_outcomes', type_='unique')
    op.drop_index('ix_story_outcomes_story_event_id', table_name='macro_story_outcomes')
    op.drop_index('ix_story_outcomes_event_type_tier', table_name='macro_story_outcomes')
    op.drop_table('macro_story_outcomes')
