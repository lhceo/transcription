"""add_okf_fields_attachments_polish_logs

Revision ID: c4f7e9a3b021
Revises: ae525721359b
Create Date: 2026-09-08 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c4f7e9a3b021'
down_revision: Union[str, Sequence[str], None] = 'ae525721359b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    from sqlalchemy import inspect as sa_inspect, text as _text
    conn = op.get_bind()
    conn.execute(_text("PRAGMA foreign_keys=OFF"))
    existing_tables = sa_inspect(conn).get_table_names()

    # ── attachments テーブル新設 ──────────────────────────────────────────
    if 'attachments' not in existing_tables:
        op.create_table(
            'attachments',
            sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
            sa.Column('project_id', sa.Integer(), nullable=True),
            sa.Column('transcript_id', sa.Integer(), nullable=True),
            sa.Column('title', sa.String(length=500), nullable=False),
            sa.Column('attachment_type', sa.String(length=20), nullable=False),
            sa.Column('url', sa.String(length=2048), nullable=True),
            sa.Column('raw_content', sa.Text(), nullable=True),
            sa.Column('processed_summary', sa.Text(), nullable=True),
            sa.Column('processed_at', sa.DateTime(timezone=True), nullable=True),
            sa.Column('created_by_user_id', sa.Integer(), nullable=False),
            sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(['created_by_user_id'], ['users.id'], ondelete='CASCADE'),
            sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='CASCADE'),
            sa.ForeignKeyConstraint(['transcript_id'], ['transcripts.id'], ondelete='CASCADE'),
            sa.PrimaryKeyConstraint('id'),
        )

    # ── polish_logs テーブル新設 ──────────────────────────────────────────
    if 'polish_logs' not in existing_tables:
        op.create_table(
            'polish_logs',
            sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
            sa.Column('transcript_id', sa.Integer(), nullable=False),
            sa.Column('model', sa.String(length=20), nullable=False),
            sa.Column('input_tokens', sa.Integer(), nullable=False),
            sa.Column('output_tokens', sa.Integer(), nullable=False),
            sa.Column('cost_yen', sa.Float(), nullable=False),
            sa.Column('created_by_user_id', sa.Integer(), nullable=False),
            sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(['created_by_user_id'], ['users.id'], ondelete='CASCADE'),
            sa.ForeignKeyConstraint(['transcript_id'], ['transcripts.id'], ondelete='CASCADE'),
            sa.PrimaryKeyConstraint('id'),
        )

    # ── project_vocabulary に meaning カラム追加 ──────────────────────────
    vocab_cols = {c['name'] for c in sa_inspect(conn).get_columns('project_vocabulary')}
    if 'meaning' not in vocab_cols:
        with op.batch_alter_table('project_vocabulary', schema=None) as batch_op:
            batch_op.add_column(sa.Column('meaning', sa.Text(), nullable=True))

    # ── transcripts に OKF フィールドと last_polished_at 追加 ─────────────
    transcript_cols = {c['name'] for c in sa_inspect(conn).get_columns('transcripts')}
    with op.batch_alter_table('transcripts', schema=None) as batch_op:
        if 'meeting_date' not in transcript_cols:
            batch_op.add_column(sa.Column('meeting_date', sa.DateTime(timezone=True), nullable=True))
        if 'meeting_location' not in transcript_cols:
            batch_op.add_column(sa.Column('meeting_location', sa.String(length=200), nullable=True))
        if 'meeting_purpose' not in transcript_cols:
            batch_op.add_column(sa.Column('meeting_purpose', sa.Text(), nullable=True))
        if 'meeting_agenda' not in transcript_cols:
            batch_op.add_column(sa.Column('meeting_agenda', sa.Text(), nullable=True))
        if 'last_polished_at' not in transcript_cols:
            batch_op.add_column(sa.Column('last_polished_at', sa.DateTime(timezone=True), nullable=True))

    conn.execute(_text("PRAGMA foreign_keys=ON"))


def downgrade() -> None:
    with op.batch_alter_table('transcripts', schema=None) as batch_op:
        batch_op.drop_column('last_polished_at')
        batch_op.drop_column('meeting_agenda')
        batch_op.drop_column('meeting_purpose')
        batch_op.drop_column('meeting_location')
        batch_op.drop_column('meeting_date')

    with op.batch_alter_table('project_vocabulary', schema=None) as batch_op:
        batch_op.drop_column('meaning')

    op.drop_table('polish_logs')
    op.drop_table('attachments')
