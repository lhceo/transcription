"""add_last_seen_at_to_users

Revision ID: d1a9f3c8e042
Revises: c4f7e9a3b021
Create Date: 2026-09-08 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect as sa_inspect


revision: str = 'd1a9f3c8e042'
down_revision: Union[str, Sequence[str], None] = 'c4f7e9a3b021'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa_inspect(bind)
    existing = [c["name"] for c in inspector.get_columns("users")]
    if "last_seen_at" not in existing:
        op.add_column("users", sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "last_seen_at")
