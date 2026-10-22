"""add author fields to segments comments and vocabulary

Revision ID: e5f7a9b1c3d4
Revises: d6e8f0a2b4c6
Create Date: 2026-10-22

"""
from alembic import op
import sqlalchemy as sa

revision = 'e5f7a9b1c3d4'
down_revision = 'd6e8f0a2b4c6'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('segments') as batch_op:
        batch_op.add_column(sa.Column('comment_author_user_id', sa.Integer(), nullable=True))

    with op.batch_alter_table('transcript_vocabulary') as batch_op:
        batch_op.add_column(sa.Column('created_by_user_id', sa.Integer(), nullable=True))

    with op.batch_alter_table('project_vocabulary') as batch_op:
        batch_op.add_column(sa.Column('created_by_user_id', sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('segments') as batch_op:
        batch_op.drop_column('comment_author_user_id')

    with op.batch_alter_table('transcript_vocabulary') as batch_op:
        batch_op.drop_column('created_by_user_id')

    with op.batch_alter_table('project_vocabulary') as batch_op:
        batch_op.drop_column('created_by_user_id')
