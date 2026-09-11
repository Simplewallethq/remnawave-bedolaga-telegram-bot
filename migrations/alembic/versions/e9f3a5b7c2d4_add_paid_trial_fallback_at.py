"""A/B «доступ за 1 ₽»: users.paid_trial_fallback_at — когда выдан фоллбэк-триал."""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "e9f3a5b7c2d4"
down_revision: Union[str, None] = "d8e2f4a6b1c3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("users", sa.Column("paid_trial_fallback_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "paid_trial_fallback_at")
