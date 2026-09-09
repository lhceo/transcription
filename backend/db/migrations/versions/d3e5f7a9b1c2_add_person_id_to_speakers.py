"""add_person_id_to_speakers

Revision ID: d3e5f7a9b1c2
Revises: c2d4e6f8a0b1
Create Date: 2026-09-09 02:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'd3e5f7a9b1c2'
down_revision: Union[str, Sequence[str], None] = 'c2d4e6f8a0b1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('speakers') as batch_op:
        batch_op.add_column(sa.Column('person_id', sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            'fk_speakers_person_id',
            'people',
            ['person_id'], ['id'],
            ondelete='SET NULL',
        )


def downgrade() -> None:
    with op.batch_alter_table('speakers') as batch_op:
        batch_op.drop_constraint('fk_speakers_person_id', type_='foreignkey')
        batch_op.drop_column('person_id')
