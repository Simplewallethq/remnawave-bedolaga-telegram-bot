from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "a2d7f3c8b5e1"
down_revision: Union[str, None] = "e5c8a2b7d9f1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("cryptobot_payments", sa.Column("metadata_json", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("cryptobot_payments", "metadata_json")
