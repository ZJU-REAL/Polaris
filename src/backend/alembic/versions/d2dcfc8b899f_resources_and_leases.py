"""资源/租约表 + 连接凭据多态化（R2 #677，设计报告 §14）

- ssh_credentials → connection_credentials：原表改名 + 加 kind（默认 'ssh'，存量行
  即天然合法，数据零搬迁）+ payload_encrypted（非 ssh 的 Fernet 加密 JSON 载荷）；
  username / private_key_encrypted 放开为可空（非 ssh 用不上）。
  experiments.credential_id 的 FK 随表改名自动跟随（sqlite ≥3.26 / postgres 的
  RENAME 都会改写引用方）。
- resources：主机/队列/License 池/仪器的统一登记（capacity/exclusive 供租约计数）。
- resource_leases：run 占用资源的租约行，released_at 为空即活租约。

downgrade 注意：把 username/private_key_encrypted 恢复 NOT NULL 前提是没有
非 ssh 行（有则先删，属有损回退，与本仓其他删列迁移同口径）。

Revision ID: d2dcfc8b899f
Revises: e867fcbae4ea
Create Date: 2026-09-06
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "d2dcfc8b899f"
down_revision: str | None = "e867fcbae4ea"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSON_VARIANT = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")


def upgrade() -> None:
    # 1) 凭据表多态化：改名 + kind + payload；ssh 专列放开可空
    op.rename_table("ssh_credentials", "connection_credentials")
    with op.batch_alter_table("connection_credentials") as batch:
        batch.add_column(
            sa.Column("kind", sa.String(length=16), nullable=False, server_default="ssh")
        )
        batch.add_column(sa.Column("payload_encrypted", sa.Text(), nullable=True))
        batch.alter_column("username", existing_type=sa.String(length=255), nullable=True)
        batch.alter_column("private_key_encrypted", existing_type=sa.Text(), nullable=True)
    # 索引名跟着表名走（batch 重建表后旧名仍在，显式换名）
    op.drop_index("ix_ssh_credentials_user_id", table_name="connection_credentials")
    op.create_index(
        op.f("ix_connection_credentials_user_id"), "connection_credentials", ["user_id"]
    )

    # 2) 资源登记表
    op.create_table(
        "resources",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "owner_id",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=255), nullable=False),
        # host | queue | license_pool | instrument
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("capacity", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("exclusive", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "credential_id",
            sa.Uuid(),
            sa.ForeignKey("connection_credentials.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("config", JSON_VARIANT, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(op.f("ix_resources_owner_id"), "resources", ["owner_id"])

    # 3) 租约表（released_at 为空 = 活租约；随资源/run 级联删除）
    op.create_table(
        "resource_leases",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "resource_id",
            sa.Uuid(),
            sa.ForeignKey("resources.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "run_id",
            sa.Uuid(),
            sa.ForeignKey("voyage_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("acquired_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("note", sa.String(length=255), nullable=False, server_default=""),
    )
    op.create_index(op.f("ix_resource_leases_resource_id"), "resource_leases", ["resource_id"])
    op.create_index(op.f("ix_resource_leases_run_id"), "resource_leases", ["run_id"])


def downgrade() -> None:
    op.drop_index(op.f("ix_resource_leases_run_id"), table_name="resource_leases")
    op.drop_index(op.f("ix_resource_leases_resource_id"), table_name="resource_leases")
    op.drop_table("resource_leases")
    op.drop_index(op.f("ix_resources_owner_id"), table_name="resources")
    op.drop_table("resources")

    # 有损回退：非 ssh 行装不进旧表形状，先删（与删列类迁移同口径）
    op.execute("DELETE FROM connection_credentials WHERE kind != 'ssh'")
    op.drop_index(op.f("ix_connection_credentials_user_id"), table_name="connection_credentials")
    with op.batch_alter_table("connection_credentials") as batch:
        batch.alter_column("private_key_encrypted", existing_type=sa.Text(), nullable=False)
        batch.alter_column("username", existing_type=sa.String(length=255), nullable=False)
        batch.drop_column("payload_encrypted")
        batch.drop_column("kind")
    op.rename_table("connection_credentials", "ssh_credentials")
    op.create_index(op.f("ix_ssh_credentials_user_id"), "ssh_credentials", ["user_id"])
