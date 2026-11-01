"""add_last_editor_to_segments

セグメントに最後に編集したユーザーを記録し、
共同編集で誰がどのセグメントを編集したか表示できるようにする。

Revision ID: e6a8b0c2d4f5
Revises: d4f6a8b0c2e3
Create Date: 2026-11-01
"""
from alembic import op
import sqlalchemy as sa

revision = 'e6a8b0c2d4f5'
down_revision = 'd4f6a8b0c2e3'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('segments') as batch_op:
        batch_op.add_column(sa.Column('last_editor_user_id', sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            'fk_segments_last_editor_user_id',
            'users', ['last_editor_user_id'], ['id'],
            ondelete='SET NULL',
        )


def downgrade() -> None:
    with op.batch_alter_table('segments') as batch_op:
        batch_op.drop_constraint('fk_segments_last_editor_user_id', type_='foreignkey')
        batch_op.drop_column('last_editor_user_id')
