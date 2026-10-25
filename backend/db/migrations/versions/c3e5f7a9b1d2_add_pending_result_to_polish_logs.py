"""add_pending_result_to_polish_logs

整文結果をDBに永続化する。サーバー再起動後も結果を復元できるようにする。

Revision ID: c3e5f7a9b1d2
Revises: b2d4f6a8c0e1
Create Date: 2026-10-25
"""
from alembic import op
import sqlalchemy as sa

revision = 'c3e5f7a9b1d2'
down_revision = 'b2d4f6a8c0e1'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('polish_logs') as batch_op:
        batch_op.add_column(sa.Column('pending_result', sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('polish_logs') as batch_op:
        batch_op.drop_column('pending_result')
