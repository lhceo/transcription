"""add collaboration fields (share permissions, segment review)

Revision ID: d6e8f0a2b4c6
Revises: b4d5e6f7a8c9
Create Date: 2026-10-08
"""
from alembic import op
import sqlalchemy as sa

revision = 'd6e8f0a2b4c6'
down_revision = 'b4d5e6f7a8c9'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('transcript_shares') as batch_op:
        batch_op.add_column(sa.Column('status', sa.String(20), nullable=False, server_default='invited'))
        batch_op.add_column(sa.Column('can_edit_segments', sa.Boolean(), nullable=False, server_default='1'))
        batch_op.add_column(sa.Column('can_edit_meeting_info', sa.Boolean(), nullable=False, server_default='1'))
        batch_op.add_column(sa.Column('can_polish', sa.Boolean(), nullable=False, server_default='0'))

    with op.batch_alter_table('segments') as batch_op:
        batch_op.add_column(sa.Column('reviewed_by_user_id', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('reviewed_at', sa.DateTime(timezone=True), nullable=True))
        batch_op.create_foreign_key(
            'fk_segments_reviewed_by_user_id',
            'users',
            ['reviewed_by_user_id'], ['id'],
            ondelete='SET NULL',
        )


def downgrade() -> None:
    with op.batch_alter_table('segments') as batch_op:
        batch_op.drop_constraint('fk_segments_reviewed_by_user_id', type_='foreignkey')
        batch_op.drop_column('reviewed_at')
        batch_op.drop_column('reviewed_by_user_id')

    with op.batch_alter_table('transcript_shares') as batch_op:
        batch_op.drop_column('can_polish')
        batch_op.drop_column('can_edit_meeting_info')
        batch_op.drop_column('can_edit_segments')
        batch_op.drop_column('status')
