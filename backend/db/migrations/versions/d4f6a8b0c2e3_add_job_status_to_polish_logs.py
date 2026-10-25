"""add_job_status_to_polish_logs

整文ジョブのステータスをDBに永続化し、サーバー再起動後も
実行中・中断を判別できるようにする。

Revision ID: d4f6a8b0c2e3
Revises: c3e5f7a9b1d2
Create Date: 2026-10-25
"""
from alembic import op
import sqlalchemy as sa

revision = 'd4f6a8b0c2e3'
down_revision = 'c3e5f7a9b1d2'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('polish_logs') as batch_op:
        batch_op.add_column(sa.Column('job_status', sa.String(20), nullable=False, server_default='done'))


def downgrade() -> None:
    with op.batch_alter_table('polish_logs') as batch_op:
        batch_op.drop_column('job_status')
