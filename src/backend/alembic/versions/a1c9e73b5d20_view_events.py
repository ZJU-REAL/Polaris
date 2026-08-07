"""view events

Revision ID: a1c9e73b5d20
Revises: 63133f647463
Create Date: 2026-08-06

文献库与论文的浏览事件。目标 id 不加外键：删掉的对象不该把「它曾被读过」这件事
一并抹掉，查询时按当前可见对象过滤就够了。
"""

import sqlalchemy as sa
from alembic import op

revision: str = "a1c9e73b5d20"
down_revision: str | None = "63133f647463"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "view_events",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("target_id", sa.Uuid(), nullable=False),
        sa.Column(
            "user_id",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
            index=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_view_events_kind_created", "view_events", ["kind", "created_at"])
    op.create_index("ix_view_events_target_created", "view_events", ["target_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_view_events_target_created", table_name="view_events")
    op.drop_index("ix_view_events_kind_created", table_name="view_events")
    op.drop_table("view_events")
