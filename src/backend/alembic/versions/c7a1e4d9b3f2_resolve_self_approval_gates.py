"""resolve pending idea_promotion / paper_submission gates (self-approval removed)

Picking an idea and marking a manuscript as submitted used to create a gate that the
same single user then had to approve. Both now change the status directly, and the
approve route no longer applies either kind. A gate that is still pending when the app
is upgraded would therefore stay in the approvals list forever, with its idea or
manuscript stuck half-way.

This approves every pending gate of the two kinds and applies what approval used to do:

- ``idea_promotion``: the idea becomes ``promoted`` (unless it was rejected meanwhile);
- ``paper_submission``: a manuscript still ``under_review`` becomes ``submitted``.

No schema change. Downgrade is a no-op: the old code handles decided gates fine, and
which rows were pending is not recorded.

Revision ID: c7a1e4d9b3f2
Revises: b2f6d1c93e47
Create Date: 2026-11-18 12:00:00.000000

"""

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "c7a1e4d9b3f2"
down_revision: str | None = "b2f6d1c93e47"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_JSON = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")

_gates = sa.table(
    "gates",
    sa.column("id", sa.Uuid()),
    sa.column("kind", sa.String()),
    sa.column("status", sa.String()),
    sa.column("payload", _JSON),
    sa.column("comment", sa.Text()),
    sa.column("decided_at", sa.DateTime(timezone=True)),
    sa.column("updated_at", sa.DateTime(timezone=True)),
)
_ideas = sa.table("ideas", sa.column("id", sa.Uuid()), sa.column("status", sa.String()))
_manuscripts = sa.table("manuscripts", sa.column("id", sa.Uuid()), sa.column("status", sa.String()))

_NOTE = "升级时自动通过：选中想法和标记投稿不再需要审批"


def _target_id(payload: Any, key: str) -> uuid.UUID | None:
    raw = payload.get(key) if isinstance(payload, dict) else None
    if not raw:
        return None
    try:
        return uuid.UUID(str(raw))
    except ValueError:
        return None


def upgrade() -> None:
    bind = op.get_bind()
    rows = bind.execute(
        sa.select(_gates.c.id, _gates.c.kind, _gates.c.payload).where(
            _gates.c.status == "pending",
            _gates.c.kind.in_(["idea_promotion", "paper_submission"]),
        )
    ).all()
    now = datetime.now(UTC)
    for gate_id, kind, payload in rows:
        if kind == "idea_promotion":
            idea_id = _target_id(payload, "idea_id")
            if idea_id is not None:
                bind.execute(
                    sa.update(_ideas)
                    .where(_ideas.c.id == idea_id, _ideas.c.status != "rejected")
                    .values(status="promoted")
                )
        else:
            manuscript_id = _target_id(payload, "manuscript_id")
            if manuscript_id is not None:
                bind.execute(
                    sa.update(_manuscripts)
                    .where(
                        _manuscripts.c.id == manuscript_id,
                        _manuscripts.c.status == "under_review",
                    )
                    .values(status="submitted")
                )
        bind.execute(
            sa.update(_gates)
            .where(_gates.c.id == gate_id)
            .values(status="approved", decided_at=now, updated_at=now, comment=_NOTE)
        )


def downgrade() -> None:
    pass
