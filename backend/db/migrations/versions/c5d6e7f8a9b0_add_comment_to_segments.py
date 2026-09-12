"""add comment to segments

Revision ID: c5d6e7f8a9b0
Revises: b3c4d5e6f7a8
Branch_labels = None
depends_on = None

"""
from alembic import op
import sqlalchemy as sa

revision = 'c5d6e7f8a9b0'
down_revision = 'b3c4d5e6f7a8'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('segments', sa.Column('comment', sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column('segments', 'comment')
