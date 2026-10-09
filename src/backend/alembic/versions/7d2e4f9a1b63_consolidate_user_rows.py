"""Single user: fold every user row into the local user (#850)

Polaris is a single-user local app (#842): the session is always the user
``local@polaris.desktop``. Databases carried over from a source / server build can
still hold other user rows — typically an account registered *before* local@ was
created. Every query filters by ``user_id`` / ``owner_id`` / ``author_id`` /
``created_by``, so whatever those rows own (projects, conversations, notes, personal
library, SSH credentials, ...) became invisible.

What this does:

1. **Pick the canonical user**: ``local@polaris.desktop`` if present, else the
   earliest active user (``created_at``, then ``id``; earliest of any user when none
   is active). When local@ is absent, the canonical user *becomes* local@ (email
   changed; username set to ``local`` only if nobody else has it) so
   ``/auth/local-session`` adopts it instead of creating an empty user. It is
   (re)activated.
2. **Re-point every foreign key to ``users.id``** onto the canonical user. The FKs
   are enumerated from the live schema (``inspect().get_foreign_keys`` — pragma
   foreign_key_list on sqlite), not from a hand list, so tables added later are
   covered. Two soft references without an FK constraint are handled too:
   ``review_messages.author_id`` (human messages) and ``gates.requested_by``
   (agent name or user id string).
3. **Unique constraints that would collide** when two users' rows merge (every
   unique constraint / unique index / primary key that contains the user column,
   also read from the schema — e.g. uq_daily_feed_likes, uq_user_library_dedup,
   uq_paper_user_meta, uq_user_paper_tags, uq_chat_bot_configs_user_platform,
   uq_user_publications_dedup, download_api_keys.user_id, user_author_profiles.user_id):
   the canonical user's row wins, the other user's duplicate is deleted. Non-colliding
   rows all move, so e.g. per-paper tags end up as the union. Users are merged one at
   a time (oldest first), so among the other accounts the oldest row wins. Partial
   unique indexes are treated as full ones (conservative; the only ones today are on
   llm_providers/model_routes ``owner_id IS NOT NULL``, which 3c7d9e1f5a20 already
   emptied).
4. **Merge ``users.settings``**: the canonical user's keys win; other users' keys
   fill the gaps (oldest first). This also recovers the preferences d3f9a1c7e2b4
   moved onto the earliest user when that was not local@.
5. **Deactivate** the other user rows (``is_active = false``) instead of deleting
   them, so nothing that still names them by id breaks.

Idempotent: a second run finds nothing to re-point and nothing to merge.

Downgrade is a no-op: the merge cannot be undone (we no longer know which row
belonged to whom).

Revision ID: 7d2e4f9a1b63
Revises: 3c7d9e1f5a20
Create Date: 2026-10-09
"""

import json
import uuid
import warnings
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa

from alembic import op

revision: str = "7d2e4f9a1b63"
down_revision: str | None = "3c7d9e1f5a20"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

LOCAL_USER_EMAIL = "local@polaris.desktop"
LOCAL_USERNAME = "local"

#: user references without an FK constraint: (table, column)
SOFT_REFERENCES = (("review_messages", "author_id"), ("gates", "requested_by"))


def _as_value(raw: Any) -> Any:
    """JSON columns come back as strings on sqlite, parsed values on postgres."""
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw
    return raw


def canonical_user_id(conn: sa.Connection) -> Any:
    """local@ if present, else the earliest active user, else the earliest user."""
    row = conn.execute(
        sa.text("SELECT id FROM users WHERE email = :e"), {"e": LOCAL_USER_EMAIL}
    ).first()
    if row is not None:
        return row[0]
    row = conn.execute(
        sa.text(
            "SELECT id FROM users WHERE is_active = :t ORDER BY created_at ASC, id ASC LIMIT 1"
        ),
        {"t": True},
    ).first()
    if row is None:
        row = conn.execute(
            sa.text("SELECT id FROM users ORDER BY created_at ASC, id ASC LIMIT 1")
        ).first()
    return None if row is None else row[0]


def _id_pairs(other: Any, canonical: Any) -> list[tuple[str, str]]:
    """(other, canonical) in each spelling a soft reference may use: dashed and hex."""
    try:
        o, c = uuid.UUID(str(other)), uuid.UUID(str(canonical))
    except ValueError:
        return [(str(other), str(canonical))]
    return [(str(o), str(c)), (o.hex, c.hex)]


def _user_columns(insp: Any) -> list[tuple[str, str, list[list[str]]]]:
    """Every (table, column) with an FK to users.id, plus the unique column sets
    on that table that contain the column."""
    found = []
    for table in insp.get_table_names():
        if table == "users":
            continue
        user_cols = [
            fk["constrained_columns"][0]
            for fk in insp.get_foreign_keys(table)
            if fk.get("referred_table") == "users"
            and len(fk.get("constrained_columns") or []) == 1
            and (fk.get("referred_columns") or ["id"]) == ["id"]
        ]
        if not user_cols:
            continue
        unique_sets: list[list[str]] = []
        for uc in insp.get_unique_constraints(table):
            unique_sets.append(list(uc["column_names"]))
        for ix in insp.get_indexes(table):
            if ix.get("unique"):
                unique_sets.append([c for c in ix["column_names"] if c is not None])
        pk = insp.get_pk_constraint(table).get("constrained_columns") or []
        if pk:
            unique_sets.append(list(pk))
        for col in dict.fromkeys(user_cols):
            sets = [s for s in unique_sets if col in s]
            found.append((table, col, sets))
    return found


def _repoint(conn: sa.Connection, canonical: Any, others: list[Any]) -> None:
    quote = conn.dialect.identifier_preparer.quote
    insp = sa.inspect(conn)
    with warnings.catch_warnings():
        # sqlite reflection grumbles about FKs it parses from SQL but cannot match in
        # pragma output (direction_libraries.project_id) — irrelevant here.
        warnings.simplefilter("ignore", sa.exc.SAWarning)
        columns = _user_columns(insp)
    for other in others:
        for table, col, unique_sets in columns:
            t, c = quote(table), quote(col)
            # Drop the other user's rows that would collide with the canonical
            # user's under any unique set (canonical row wins).
            for unique_set in unique_sets:
                rest = [x for x in unique_set if x != col]
                match = "".join(f" AND k.{quote(x)} = {t}.{quote(x)}" for x in rest)
                conn.execute(
                    sa.text(
                        f"DELETE FROM {t} WHERE {c} = :other AND EXISTS ("
                        f"SELECT 1 FROM {t} AS k WHERE k.{c} = :canon{match})"
                    ),
                    {"other": other, "canon": canonical},
                )
            conn.execute(
                sa.text(f"UPDATE {t} SET {c} = :canon WHERE {c} = :other"),
                {"other": other, "canon": canonical},
            )
    tables = set(insp.get_table_names())
    for table, col in SOFT_REFERENCES:
        if table not in tables or col not in {c["name"] for c in insp.get_columns(table)}:
            continue
        t, c = quote(table), quote(col)
        for other in others:
            # Keep the spelling the row already used (dashed vs hex).
            for other_form, canon_form in _id_pairs(other, canonical):
                conn.execute(
                    sa.text(f"UPDATE {t} SET {c} = :canon WHERE {c} = :other"),
                    {"other": other_form, "canon": canon_form},
                )


def _merge_settings(conn: sa.Connection, canonical: Any, others: list[Any]) -> None:
    row = conn.execute(
        sa.text("SELECT settings FROM users WHERE id = :i"), {"i": canonical}
    ).first()
    current = _as_value(row[0]) if row is not None else None
    merged = dict(current) if isinstance(current, dict) else {}
    changed = False
    for other in others:
        r = conn.execute(sa.text("SELECT settings FROM users WHERE id = :i"), {"i": other}).first()
        value = _as_value(r[0]) if r is not None else None
        if not isinstance(value, dict):
            continue
        for key, val in value.items():
            if key not in merged:
                merged[key] = val
                changed = True
    if changed:
        conn.execute(
            sa.text("UPDATE users SET settings = :s WHERE id = :i"),
            {"s": json.dumps(merged), "i": canonical},
        )


def _adopt(conn: sa.Connection, canonical: Any) -> None:
    """Make the canonical user the local user: email, free username, active."""
    email = conn.execute(
        sa.text("SELECT email FROM users WHERE id = :i"), {"i": canonical}
    ).scalar_one()
    if email != LOCAL_USER_EMAIL:
        conn.execute(
            sa.text("UPDATE users SET email = :e WHERE id = :i"),
            {"e": LOCAL_USER_EMAIL, "i": canonical},
        )
        taken = conn.execute(
            sa.text("SELECT 1 FROM users WHERE username = :u AND id != :i"),
            {"u": LOCAL_USERNAME, "i": canonical},
        ).first()
        if taken is None:
            conn.execute(
                sa.text("UPDATE users SET username = :u WHERE id = :i"),
                {"u": LOCAL_USERNAME, "i": canonical},
            )
    conn.execute(
        sa.text("UPDATE users SET is_active = :t WHERE id = :i"), {"t": True, "i": canonical}
    )


def consolidate(conn: sa.Connection) -> None:
    """The whole upgrade, on a plain connection (tests run it on a metadata-built DB)."""
    canonical = canonical_user_id(conn)
    if canonical is None:
        return
    others = [
        row[0]
        for row in conn.execute(
            sa.text("SELECT id FROM users WHERE id != :c ORDER BY created_at ASC, id ASC"),
            {"c": canonical},
        )
    ]
    _adopt(conn, canonical)
    if not others:
        return
    _repoint(conn, canonical, others)
    _merge_settings(conn, canonical, others)
    conn.execute(
        sa.text("UPDATE users SET is_active = :f WHERE id != :c"), {"f": False, "c": canonical}
    )


def upgrade() -> None:
    consolidate(op.get_bind())


def downgrade() -> None:
    """No-op: merged rows cannot be handed back to the accounts they came from."""
