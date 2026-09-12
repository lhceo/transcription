"""add is_polished to segments

Revision ID: b3c4d5e6f7a8
Revises: c4d5e6f7a8b9
Branch_labels = None
depends_on = None

"""
from alembic import op
import sqlalchemy as sa

revision = 'b3c4d5e6f7a8'
down_revision = 'c4d5e6f7a8b9'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('segments', sa.Column('is_polished', sa.Boolean(), nullable=False, server_default='0'))


def downgrade() -> None:
    op.drop_column('segments', 'is_polished')
