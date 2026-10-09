"""单用户：LLM 配置并成一张表，外部 agent 去掉「共享」列（#842）

Polaris 只在本机运行、只有一个本地用户，「按人配置」（#801）和「共享给其他账号」
（#836）都没有对象了。

**llm_providers / model_routes**：#801 起这两张表按 ``owner_id`` 分成「部署级」
（NULL）和「某个用户自己的」两份。现在只认 NULL 那份，所以把按人的行并进来——但只并
本地用户自己的那份（#850）：

- 「本地用户」与 7d2e4f9a1b63 合并用户行用同一条规则：``local@polaris.desktop``，
  没有它时取最早建的活跃用户。**别的账号的按人 provider / 路由直接删掉**：它们存着
  别人的 API key，不能并进这台机器唯一的那张表；引用了被删 provider 的路由一并删。
- model_routes：同一环节已有部署级路由的，留部署级、删本地用户那条（部署级一直是
  主人在设置页里看到和编辑的那份）；没有的，本地用户那条改成 NULL。
- llm_providers：本地用户的一律改成 NULL。名字在 NULL 行里唯一（部分唯一索引
  uq_providers_global_name），撞名的在后面加「 (2)」「 (3)」……直到不撞。
  路由按 provider id 引用，改名不影响。

``owner_id`` 列和那两条部分唯一索引都留着：删列在 sqlite 上要重建表，而且列留着
无害——代码只读写 NULL 行。

**acp_agents.shared**：删列。降级时建回来，一律 false（原值丢了：单用户下它本来
就不影响任何行为）。LLM 行的合并不可逆：降级不会把行还给原来的人。

Revision ID: 3c7d9e1f5a20
Revises: 88a5bdc0aa15
Create Date: 2026-10-09
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "3c7d9e1f5a20"
down_revision: str | None = "88a5bdc0aa15"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

LOCAL_USER_EMAIL = "local@polaris.desktop"


def _canonical_user_id(conn: sa.Connection):
    """local@ if present, else the earliest active user, else the earliest user
    (the same rule as 7d2e4f9a1b63 / services/local_user.py)."""
    row = conn.execute(
        sa.text("SELECT id FROM users WHERE email = :e"), {"e": LOCAL_USER_EMAIL}
    ).first()
    if row is None:
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


def _drop_other_accounts(conn: sa.Connection) -> None:
    """Delete every per-user provider / route that is not the local user's."""
    canonical = _canonical_user_id(conn)
    if canonical is None:
        other, params = "owner_id IS NOT NULL", {}
    else:
        other, params = "owner_id IS NOT NULL AND owner_id != :canon", {"canon": canonical}
    conn.execute(
        sa.text(
            f"DELETE FROM model_routes WHERE ({other}) OR provider_id IN "
            f"(SELECT id FROM llm_providers WHERE {other})"
        ),
        params,
    )
    conn.execute(sa.text(f"DELETE FROM llm_providers WHERE {other}"), params)


def _merge_routes(conn: sa.Connection) -> None:
    platform_stages = {
        row[0]
        for row in conn.execute(sa.text("SELECT stage FROM model_routes WHERE owner_id IS NULL"))
    }
    # Only the local user's rows are left: at most one per stage (uq_routes_owner_stage).
    per_user = conn.execute(
        sa.text(
            "SELECT id, stage FROM model_routes WHERE owner_id IS NOT NULL ORDER BY created_at, id"
        )
    ).all()
    for route_id, stage in per_user:
        if stage in platform_stages:
            conn.execute(sa.text("DELETE FROM model_routes WHERE id = :id"), {"id": route_id})
        else:
            conn.execute(
                sa.text("UPDATE model_routes SET owner_id = NULL WHERE id = :id"), {"id": route_id}
            )
            platform_stages.add(stage)


def _merge_providers(conn: sa.Connection) -> None:
    taken = {
        row[0]
        for row in conn.execute(sa.text("SELECT name FROM llm_providers WHERE owner_id IS NULL"))
    }
    per_user = conn.execute(
        sa.text(
            "SELECT id, name FROM llm_providers WHERE owner_id IS NOT NULL ORDER BY created_at, id"
        )
    ).all()
    for provider_id, name in per_user:
        new_name = name
        n = 2
        while new_name in taken:
            suffix = f" ({n})"
            new_name = f"{name[: 255 - len(suffix)]}{suffix}"
            n += 1
        taken.add(new_name)
        conn.execute(
            sa.text("UPDATE llm_providers SET owner_id = NULL, name = :name WHERE id = :id"),
            {"id": provider_id, "name": new_name},
        )


def upgrade() -> None:
    conn = op.get_bind()
    _drop_other_accounts(conn)
    _merge_routes(conn)
    _merge_providers(conn)
    with op.batch_alter_table("acp_agents") as batch:
        batch.drop_column("shared")


def downgrade() -> None:
    with op.batch_alter_table("acp_agents") as batch:
        batch.add_column(
            sa.Column("shared", sa.Boolean(), nullable=False, server_default=sa.false())
        )
