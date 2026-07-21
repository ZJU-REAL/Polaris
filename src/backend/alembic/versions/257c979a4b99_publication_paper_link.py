"""user_publications.paper_id soft link to library papers

Revision ID: 257c979a4b99
Revises: c98c3216fc0a
Create Date: 2026-07-21 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "257c979a4b99"
down_revision: str | None = "c98c3216fc0a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("user_publications") as batch:
        batch.add_column(sa.Column("paper_id", sa.Uuid(), nullable=True))
        batch.create_foreign_key(
            "fk_user_publications_paper_id", "papers", ["paper_id"], ["id"], ondelete="SET NULL"
        )


def downgrade() -> None:
    with op.batch_alter_table("user_publications") as batch:
        batch.drop_constraint("fk_user_publications_paper_id", type_="foreignkey")
        batch.drop_column("paper_id")
