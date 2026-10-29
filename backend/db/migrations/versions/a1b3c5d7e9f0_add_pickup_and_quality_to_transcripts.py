"""add pickup_suggestions and quality_check_result to transcripts

Revision ID: a1b3c5d7e9f0
Revises: f7b9c1d3e5a6
Create Date: 2026-10-24

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.engine.reflection import Inspector

revision = 'a1b3c5d7e9f0'
down_revision = 'f7b9c1d3e5a6'
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = Inspector.from_engine(bind)
    columns = [c['name'] for c in inspector.get_columns('transcripts')]

    with op.batch_alter_table('transcripts') as batch_op:
        if 'pickup_suggestions' not in columns:
            batch_op.add_column(sa.Column('pickup_suggestions', sa.Text(), nullable=True))
        if 'quality_check_result' not in columns:
            batch_op.add_column(sa.Column('quality_check_result', sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('transcripts') as batch_op:
        batch_op.drop_column('quality_check_result')
        batch_op.drop_column('pickup_suggestions')
