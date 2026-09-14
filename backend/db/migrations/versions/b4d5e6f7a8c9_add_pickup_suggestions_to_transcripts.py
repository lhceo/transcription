"""add pickup_suggestions to transcripts

Revision ID: b4d5e6f7a8c9
Revises: a3c5e7b9d1f2
Create Date: 2026-09-14
"""
from alembic import op
import sqlalchemy as sa

revision = 'b4d5e6f7a8c9'
down_revision = 'a3c5e7b9d1f2'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('transcripts', sa.Column('pickup_suggestions', sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column('transcripts', 'pickup_suggestions')
