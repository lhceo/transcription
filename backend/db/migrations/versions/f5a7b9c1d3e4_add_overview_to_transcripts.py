"""add overview to transcripts

Revision ID: f5a7b9c1d3e4
Revises: e4f6a8b0c2d3
Create Date: 2026-09-11
"""
from alembic import op
import sqlalchemy as sa

revision = 'f5a7b9c1d3e4'
down_revision = 'e4f6a8b0c2d3'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('transcripts', sa.Column('overview', sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column('transcripts', 'overview')
