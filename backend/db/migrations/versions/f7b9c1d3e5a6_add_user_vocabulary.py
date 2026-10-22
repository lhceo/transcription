"""add user_vocabulary table for cross-project common vocabulary

Revision ID: f7b9c1d3e5a6
Revises: e5f7a9b1c3d4
Create Date: 2026-10-23

"""
from alembic import op
import sqlalchemy as sa

revision = 'f7b9c1d3e5a6'
down_revision = 'e5f7a9b1c3d4'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'user_vocabulary',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('word', sa.String(200), nullable=False),
        sa.Column('meaning', sa.Text(), nullable=True),
        sa.Column('reading', sa.String(200), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], name='fk_user_vocab_user_id', ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )


def downgrade() -> None:
    op.drop_table('user_vocabulary')
