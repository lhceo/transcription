"""add_reading_to_project_vocabulary

Revision ID: e4f6a8b0c2d3
Revises: d3e5f7a9b1c2
Create Date: 2026-09-09 03:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'e4f6a8b0c2d3'
down_revision: Union[str, Sequence[str], None] = 'd3e5f7a9b1c2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('project_vocabulary') as batch_op:
        batch_op.add_column(sa.Column('reading', sa.String(200), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('project_vocabulary') as batch_op:
        batch_op.drop_column('reading')
