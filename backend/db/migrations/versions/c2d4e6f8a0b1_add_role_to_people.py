"""add_role_to_people

Revision ID: c2d4e6f8a0b1
Revises: b1c2d3e4f5a6
Create Date: 2026-09-09 01:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c2d4e6f8a0b1'
down_revision: Union[str, Sequence[str], None] = 'b1c2d3e4f5a6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('people') as batch_op:
        batch_op.add_column(sa.Column('role', sa.String(200), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('people') as batch_op:
        batch_op.drop_column('role')
