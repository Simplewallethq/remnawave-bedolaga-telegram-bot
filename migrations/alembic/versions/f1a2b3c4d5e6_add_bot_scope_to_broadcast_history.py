"""Область ботов рассылки: broadcast_history.bot_scope — leto | all | copycat:<bot_id>."""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "f1a2b3c4d5e6"
down_revision: Union[str, None] = "e9f3a5b7c2d4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("broadcast_history", sa.Column("bot_scope", sa.String(length=32), nullable=True))


def downgrade() -> None:
    op.drop_column("broadcast_history", "bot_scope")
