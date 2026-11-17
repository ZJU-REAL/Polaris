"""paper_wikis.evidence: evidence locators leave the summary text (#873)

The wiki compiler used to append the evidence locators to the summary itself, as an
HTML comment ``<!-- polaris-ai-evidence:{json} -->``. The summary is user-visible
text: the markdown renderer shows the comment as plain text (the "garbled string"
after a summary), and the same JSON went into exports, search and model context.

This adds ``paper_wikis.evidence`` (JSON) and, for every stored summary that carries
the comment, moves the JSON into the column and removes the comment from the text.
A comment whose JSON does not parse is still removed.

Downgrade puts the locators back at the end of the text in the old format, then
drops the column.

Revision ID: b2f6d1c93e47
Revises: 9a4c2e7b1d58
Create Date: 2026-11-18 00:00:00.000000

"""

import json
import re
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "b2f6d1c93e47"
down_revision: str | None = "9a4c2e7b1d58"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 与 app/core/wiki_evidence.py 同一口径；迁移不 import app 代码（那边以后会变）
_MARKER = "<!-- polaris-ai-evidence:"
_MANIFEST_RE = re.compile(r"\s*<!-- polaris-ai-evidence:(\{.*?\}) -->[ \t]*\n?", re.DOTALL)

_JSON = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")

_wikis = sa.table(
    "paper_wikis",
    sa.column("id", sa.Uuid()),
    sa.column("content", sa.Text()),
    sa.column("evidence", _JSON),
)


def _split(text: str) -> tuple[str, dict[str, Any] | None]:
    evidence: dict[str, Any] | None = None
    for match in _MANIFEST_RE.finditer(text):
        try:
            parsed = json.loads(match.group(1))
        except ValueError:
            continue
        if isinstance(parsed, dict):
            evidence = parsed
    return _MANIFEST_RE.sub("\n\n", text).rstrip(), evidence


def upgrade() -> None:
    op.add_column("paper_wikis", sa.Column("evidence", _JSON, nullable=True))
    bind = op.get_bind()
    rows = bind.execute(
        sa.select(_wikis.c.id, _wikis.c.content).where(_wikis.c.content.contains(_MARKER))
    ).all()
    for wiki_id, content in rows:
        body, evidence = _split(content or "")
        # evidence 为 None 时不写这一列：JSON 列写 None 落的是 JSON 的 null 而不是 SQL NULL
        values: dict[str, Any] = {"content": body}
        if evidence is not None:
            values["evidence"] = evidence
        bind.execute(sa.update(_wikis).where(_wikis.c.id == wiki_id).values(**values))


def downgrade() -> None:
    bind = op.get_bind()
    rows = bind.execute(
        sa.select(_wikis.c.id, _wikis.c.content, _wikis.c.evidence).where(
            _wikis.c.evidence.is_not(None)
        )
    ).all()
    for wiki_id, content, evidence in rows:
        if not isinstance(evidence, dict):
            continue
        payload = json.dumps(evidence, ensure_ascii=False, separators=(",", ":"))
        payload = payload.replace("-->", "\\u002d\\u002d\\u003e")
        bind.execute(
            sa.update(_wikis)
            .where(_wikis.c.id == wiki_id)
            .values(content=f"{(content or '').rstrip()}\n\n{_MARKER}{payload} -->\n")
        )
    with op.batch_alter_table("paper_wikis") as batch:
        batch.drop_column("evidence")
