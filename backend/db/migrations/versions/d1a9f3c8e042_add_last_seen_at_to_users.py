"""add_last_seen_at_to_users

Revision ID: d1a9f3c8e042
Revises: c4f7e9a3b021
Create Date: 2026-09-08 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect as sa_inspect, text as _text


revision: str = 'd1a9f3c8e042'
down_revision: Union[str, Sequence[str], None] = 'c4f7e9a3b021'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    conn.execute(_text("PRAGMA foreign_keys=OFF"))
    cols = {c['name'] for c in sa_inspect(conn).get_columns('users')}
    with op.batch_alter_table('users', schema=None) as batch_op:
        if 'last_seen_at' not in cols:
            batch_op.add_column(sa.Column('last_seen_at', sa.DateTime(timezone=True), nullable=True))
    conn.execute(_text("PRAGMA foreign_keys=ON"))


def downgrade() -> None:
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_column('last_seen_at')
