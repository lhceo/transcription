"""add_transcript_shares

Revision ID: f3c9e2a7b165
Revises: e2b8f1c9d054
Create Date: 2026-09-08 19:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import text as _text


revision: str = 'f3c9e2a7b165'
down_revision: Union[str, Sequence[str], None] = 'e2b8f1c9d054'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    conn.execute(_text("PRAGMA foreign_keys=OFF"))
    op.create_table(
        'transcript_shares',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('transcript_id', sa.Integer(), nullable=False),
        sa.Column('shared_with_user_id', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['transcript_id'], ['transcripts.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['shared_with_user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('transcript_id', 'shared_with_user_id', name='uq_transcript_share'),
    )
    op.create_index('ix_transcript_shares_user', 'transcript_shares', ['shared_with_user_id'])
    conn.execute(_text("PRAGMA foreign_keys=ON"))


def downgrade() -> None:
    op.drop_table('transcript_shares')
