"""add pre_polish_text to segments

Revision ID: a3c5e7b9d1f2
Revises: f5a7b9c1d3e4
Create Date: 2026-09-14
"""
from alembic import op
import sqlalchemy as sa

revision = 'a3c5e7b9d1f2'
down_revision = 'f5a7b9c1d3e4'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('segments', sa.Column('pre_polish_text', sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column('segments', 'pre_polish_text')
