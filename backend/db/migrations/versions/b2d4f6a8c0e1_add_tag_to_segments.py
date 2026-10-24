"""add tag column to segments for decision/todo labeling

Revision ID: b2d4f6a8c0e1
Revises: a1b3c5d7e9f0
Create Date: 2026-10-24

"""
from alembic import op
import sqlalchemy as sa

revision = 'b2d4f6a8c0e1'
down_revision = 'a1b3c5d7e9f0'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('segments') as batch_op:
        batch_op.add_column(sa.Column('tag', sa.String(20), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('segments') as batch_op:
        batch_op.drop_column('tag')
