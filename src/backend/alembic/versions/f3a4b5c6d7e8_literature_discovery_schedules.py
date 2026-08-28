"""Schedule versioned incremental literature discovery runs."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "f3a4b5c6d7e8"
down_revision: str = "f2a3b4c5d6e7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "literature_search_runs",
        sa.Column("trigger", sa.String(16), nullable=False, server_default="manual"),
    )
    op.add_column("literature_search_runs", sa.Column("schedule_version", sa.Integer()))
    op.add_column(
        "literature_search_runs", sa.Column("scheduled_for", sa.DateTime(timezone=True))
    )
    op.create_index(
        "uq_literature_search_runs_active_schedule",
        "literature_search_runs",
        ["library_id"],
        unique=True,
        postgresql_where=sa.text(
            "trigger = 'scheduled' AND status IN ('queued', 'running')"
        ),
        sqlite_where=sa.text("trigger = 'scheduled' AND status IN ('queued', 'running')"),
    )
    op.create_table(
        "literature_discovery_schedules",
        sa.Column(
            "library_id",
            sa.Uuid(),
            sa.ForeignKey("direction_libraries.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("timezone", sa.String(64), nullable=False),
        sa.Column("hour", sa.Integer(), nullable=False),
        sa.Column("minute", sa.Integer(), nullable=False),
        sa.Column("requested_count", sa.Integer(), nullable=False),
        sa.Column("candidate_budget", sa.Integer(), nullable=False),
        sa.Column("start_year", sa.Integer()),
        sa.Column("end_year", sa.Integer()),
        sa.Column("config_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "created_by",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
        ),
        sa.Column("next_run_at", sa.DateTime(timezone=True)),
        sa.Column(
            "last_run_id",
            sa.Uuid(),
            sa.ForeignKey("literature_search_runs.id", ondelete="SET NULL"),
        ),
        sa.Column("last_enqueued_at", sa.DateTime(timezone=True)),
        sa.Column("last_error_code", sa.String(64)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_literature_discovery_schedules_created_by",
        "literature_discovery_schedules",
        ["created_by"],
    )
    op.create_index(
        "ix_literature_discovery_schedules_last_run_id",
        "literature_discovery_schedules",
        ["last_run_id"],
    )
    op.create_index(
        "ix_literature_discovery_schedules_due",
        "literature_discovery_schedules",
        ["enabled", "next_run_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_literature_discovery_schedules_due",
        table_name="literature_discovery_schedules",
    )
    op.drop_index(
        "ix_literature_discovery_schedules_last_run_id",
        table_name="literature_discovery_schedules",
    )
    op.drop_index(
        "ix_literature_discovery_schedules_created_by",
        table_name="literature_discovery_schedules",
    )
    op.drop_table("literature_discovery_schedules")
    op.drop_index(
        "uq_literature_search_runs_active_schedule",
        table_name="literature_search_runs",
    )
    op.drop_column("literature_search_runs", "scheduled_for")
    op.drop_column("literature_search_runs", "schedule_version")
    op.drop_column("literature_search_runs", "trigger")
