"""add is_bookmarked to segments

Revision ID: c4d5e6f7a8b9
Revises: f5a7b9c1d3e4
Create Date: 2026-09-12
"""
from alembic import op
import sqlalchemy as sa

revision = 'c4d5e6f7a8b9'
down_revision = 'b2c3d4e5f6a7'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('segments', sa.Column('is_bookmarked', sa.Boolean(), nullable=False, server_default='0'))
    op.add_column('segments', sa.Column('bookmark_memo', sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column('segments', 'bookmark_memo')
    op.drop_column('segments', 'is_bookmarked')
