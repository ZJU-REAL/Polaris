"""Add normalized paper identifiers.

Revision ID: 8c4a6f1d2e90
Revises: 8ff89f7fcdeb
Create Date: 2026-08-12
"""

import json
import logging
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "8c4a6f1d2e90"
down_revision: str | None = "8ff89f7fcdeb"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "paper_identifiers",
        sa.Column("paper_id", sa.Uuid(), nullable=False),
        sa.Column("namespace", sa.String(length=32), nullable=False),
        sa.Column("raw_value", sa.String(length=512), nullable=False),
        sa.Column("normalized_value", sa.String(length=512), nullable=False),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("is_verified", sa.Boolean(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["paper_id"], ["papers.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "namespace", "normalized_value", name="uq_paper_identifier_value"
        ),
        sa.UniqueConstraint(
            "paper_id", "namespace", "normalized_value", name="uq_paper_identifier"
        ),
    )
    op.create_index(
        "ix_paper_identifiers_paper_id", "paper_identifiers", ["paper_id"], unique=False
    )
    op.create_index(
        "ix_paper_identifiers_paper_namespace",
        "paper_identifiers",
        ["paper_id", "namespace"],
        unique=False,
    )

    bind = op.get_bind()
    paper_rows = bind.execute(
        sa.text(
            "SELECT id, arxiv_id, doi, external_ids, source FROM papers "
            "ORDER BY created_at, id"
        )
    ).mappings()
    identifiers = sa.table(
        "paper_identifiers",
        sa.column("id", sa.Uuid()),
        sa.column("paper_id", sa.Uuid()),
        sa.column("namespace", sa.String()),
        sa.column("raw_value", sa.String()),
        sa.column("normalized_value", sa.String()),
        sa.column("source", sa.String()),
        sa.column("confidence", sa.Float()),
        sa.column("is_verified", sa.Boolean()),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    import datetime
    import uuid

    now = datetime.datetime.now(datetime.UTC)
    claimed_by: dict[tuple[str, str], uuid.UUID] = {}
    collision_count = 0
    for paper in paper_rows:
        paper_id = paper["id"]
        if not isinstance(paper_id, uuid.UUID):
            paper_id = uuid.UUID(str(paper_id))
        raw_external_ids = paper["external_ids"] or {}
        if isinstance(raw_external_ids, str):
            try:
                raw_external_ids = json.loads(raw_external_ids)
            except (TypeError, ValueError):
                raw_external_ids = {}
        values = dict(raw_external_ids) if isinstance(raw_external_ids, dict) else {}
        if paper["arxiv_id"]:
            values["arxiv"] = paper["arxiv_id"]
        if paper["doi"]:
            values["doi"] = paper["doi"]
        seen: set[tuple[str, str]] = set()
        for namespace, raw_value in values.items():
            if raw_value is None:
                continue
            namespace = str(namespace).strip().lower().replace("semantic_scholar", "s2")
            if namespace == "corpus_id":
                namespace = "s2_corpus"
            raw_value = str(raw_value).strip()
            normalized = raw_value.lower()
            if namespace == "doi":
                for prefix in ("https://doi.org/", "http://doi.org/", "doi:"):
                    if normalized.startswith(prefix):
                        normalized = normalized[len(prefix) :].strip()
                        break
            elif namespace == "arxiv":
                normalized = normalized.removeprefix("https://arxiv.org/abs/")
                if "v" in normalized and normalized.rsplit("v", 1)[-1].isdigit():
                    normalized = normalized.rsplit("v", 1)[0]
            elif namespace == "pmcid":
                normalized = normalized.upper()
                if normalized and not normalized.startswith("PMC"):
                    normalized = f"PMC{normalized}"
            key = (namespace, normalized)
            if not namespace or not normalized or key in seen:
                continue
            seen.add(key)
            if key in claimed_by:
                collision_count += 1
                continue
            claimed_by[key] = paper_id
            bind.execute(
                identifiers.insert().values(
                    id=uuid.uuid4(),
                    paper_id=paper_id,
                    namespace=namespace,
                    raw_value=raw_value,
                    normalized_value=normalized,
                    source=paper["source"] or "legacy",
                    confidence=1.0,
                    is_verified=False,
                    created_at=now,
                    updated_at=now,
                )
            )
    if collision_count:
        logging.getLogger("alembic.runtime.migration").warning(
            "paper_identifiers backfill skipped %d identifier collision(s)",
            collision_count,
        )


def downgrade() -> None:
    op.drop_index("ix_paper_identifiers_paper_namespace", table_name="paper_identifiers")
    op.drop_index("ix_paper_identifiers_paper_id", table_name="paper_identifiers")
    op.drop_table("paper_identifiers")
