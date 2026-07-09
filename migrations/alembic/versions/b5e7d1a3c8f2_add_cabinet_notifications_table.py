"""add cabinet notifications table"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.engine.reflection import Inspector


revision: str = 'b5e7d1a3c8f2'
down_revision: Union[str, None] = 'a2d4f6b8c1e3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


TABLE_NAME = 'cabinet_notifications'
INDEXES = (
    ('ix_cabinet_notifications_user_created', ['user_id', 'created_at']),
    ('ix_cabinet_notifications_user_unread', ['user_id', 'is_read']),
)


def _table_exists(inspector: Inspector) -> bool:
    return TABLE_NAME in inspector.get_table_names()


def _existing_indexes(inspector: Inspector) -> set:
    return {index['name'] for index in inspector.get_indexes(TABLE_NAME)}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not _table_exists(inspector):
        op.create_table(
            TABLE_NAME,
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column(
                'user_id',
                sa.Integer(),
                sa.ForeignKey('users.id', ondelete='CASCADE'),
                nullable=False,
            ),
            sa.Column('type', sa.String(length=50), nullable=False),
            sa.Column('title', sa.String(length=255), nullable=True),
            sa.Column('body', sa.Text(), nullable=True),
            sa.Column('payload', sa.JSON(), nullable=True),
            sa.Column('is_read', sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column('read_at', sa.DateTime(), nullable=True),
            sa.Column('created_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
        )
        inspector = sa.inspect(bind)

    existing = _existing_indexes(inspector)
    for index_name, columns in INDEXES:
        if index_name not in existing:
            op.create_index(index_name, TABLE_NAME, columns)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if _table_exists(inspector):
        op.drop_table(TABLE_NAME)
