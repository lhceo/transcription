"""add original_asr_text to segments

Revision ID: a1b2c3d4e5f6
Revises: f5a7b9c1d3e4
Create Date: 2026-09-12

"""
from alembic import op
import sqlalchemy as sa

revision = 'a1b2c3d4e5f6'
down_revision = 'f5a7b9c1d3e4'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('segments', sa.Column('original_asr_text', sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column('segments', 'original_asr_text')
