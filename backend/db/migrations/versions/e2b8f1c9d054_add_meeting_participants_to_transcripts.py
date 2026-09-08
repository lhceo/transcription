"""add_meeting_participants_to_transcripts

Revision ID: e2b8f1c9d054
Revises: d1a9f3c8e042
Create Date: 2026-09-08 18:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect as sa_inspect, text as _text


revision: str = 'e2b8f1c9d054'
down_revision: Union[str, Sequence[str], None] = 'd1a9f3c8e042'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    conn.execute(_text("PRAGMA foreign_keys=OFF"))
    cols = {c['name'] for c in sa_inspect(conn).get_columns('transcripts')}
    with op.batch_alter_table('transcripts', schema=None) as batch_op:
        if 'meeting_participants' not in cols:
            batch_op.add_column(sa.Column('meeting_participants', sa.Text(), nullable=True))
    conn.execute(_text("PRAGMA foreign_keys=ON"))


def downgrade() -> None:
    with op.batch_alter_table('transcripts', schema=None) as batch_op:
        batch_op.drop_column('meeting_participants')
