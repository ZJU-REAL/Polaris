"""experiment lab (M4): ssh_credentials table, experiment/run columns per docs/api-m4.md

- ssh_credentials：每用户 SSH 凭据（私钥/口令 Fernet 加密列）
- experiments：project_id/voyage_id/credential_id 外键、report、metrics
- experiment_runs：seq（experiment 内唯一）、exit_code、pid、started_at/finished_at

Revision ID: a9b0c1d2e3f4
Revises: e8f9a0b1c2d3
Create Date: 2026-07-14
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "a9b0c1d2e3f4"
down_revision: str | None = "e8f9a0b1c2d3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSONVariant = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")


def upgrade() -> None:
    op.create_table(
        "ssh_credentials",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("host", sa.String(length=255), nullable=False),
        sa.Column("port", sa.Integer(), nullable=False),
        sa.Column("username", sa.String(length=255), nullable=False),
        sa.Column("private_key_encrypted", sa.Text(), nullable=False),
        sa.Column("passphrase_encrypted", sa.Text(), nullable=True),
        sa.Column("last_verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_ssh_credentials_user_id"), "ssh_credentials", ["user_id"], unique=False
    )

    # experiments 此前无写入口（M0 仅建模），表为空，NOT NULL 新列可直接加
    with op.batch_alter_table("experiments") as batch:
        batch.add_column(sa.Column("project_id", sa.Uuid(), nullable=False))
        batch.add_column(sa.Column("voyage_id", sa.Uuid(), nullable=True))
        batch.add_column(sa.Column("credential_id", sa.Uuid(), nullable=True))
        batch.add_column(sa.Column("report", sa.Text(), nullable=True))
        batch.add_column(sa.Column("metrics", JSONVariant, nullable=True))
        batch.create_foreign_key(
            "fk_experiments_project_id", "projects", ["project_id"], ["id"], ondelete="CASCADE"
        )
        batch.create_foreign_key(
            "fk_experiments_voyage_id", "voyage_runs", ["voyage_id"], ["id"], ondelete="SET NULL"
        )
        batch.create_foreign_key(
            "fk_experiments_credential_id",
            "ssh_credentials",
            ["credential_id"],
            ["id"],
            ondelete="SET NULL",
        )
    op.create_index(op.f("ix_experiments_project_id"), "experiments", ["project_id"], unique=False)
    op.create_index(op.f("ix_experiments_voyage_id"), "experiments", ["voyage_id"], unique=False)

    with op.batch_alter_table("experiment_runs") as batch:
        batch.add_column(sa.Column("seq", sa.Integer(), nullable=False, server_default="1"))
        batch.add_column(sa.Column("exit_code", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("pid", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("started_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True))
        batch.create_unique_constraint("uq_experiment_runs_exp_seq", ["experiment_id", "seq"])


def downgrade() -> None:
    with op.batch_alter_table("experiment_runs") as batch:
        batch.drop_constraint("uq_experiment_runs_exp_seq", type_="unique")
        batch.drop_column("finished_at")
        batch.drop_column("started_at")
        batch.drop_column("pid")
        batch.drop_column("exit_code")
        batch.drop_column("seq")

    op.drop_index(op.f("ix_experiments_voyage_id"), table_name="experiments")
    op.drop_index(op.f("ix_experiments_project_id"), table_name="experiments")
    with op.batch_alter_table("experiments") as batch:
        batch.drop_constraint("fk_experiments_credential_id", type_="foreignkey")
        batch.drop_constraint("fk_experiments_voyage_id", type_="foreignkey")
        batch.drop_constraint("fk_experiments_project_id", type_="foreignkey")
        batch.drop_column("metrics")
        batch.drop_column("report")
        batch.drop_column("credential_id")
        batch.drop_column("voyage_id")
        batch.drop_column("project_id")

    op.drop_index(op.f("ix_ssh_credentials_user_id"), table_name="ssh_credentials")
    op.drop_table("ssh_credentials")
