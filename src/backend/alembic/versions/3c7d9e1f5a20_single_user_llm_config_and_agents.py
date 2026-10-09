"""单用户：LLM 配置并成一张表，外部 agent 去掉「共享」列（#842）

Polaris 只在本机运行、只有一个本地用户，「按人配置」（#801）和「共享给其他账号」
（#836）都没有对象了。

**llm_providers / model_routes**：#801 起这两张表按 ``owner_id`` 分成「部署级」
（NULL）和「某个用户自己的」两份。现在只认 NULL 那份，所以把按人的行并进来：

- model_routes：同一环节已有部署级路由的，留部署级、删按人的那条（部署级一直是
  主人在设置页里看到和编辑的那份）；没有的，按人那条改成 NULL。同一环节有好几个
  人各配了一条时，留本地用户（local@polaris.desktop）的，其次留最早建的。
- llm_providers：一律改成 NULL。名字在 NULL 行里唯一（部分唯一索引
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


def _merge_routes(conn: sa.Connection) -> None:
    platform_stages = {
        row[0]
        for row in conn.execute(sa.text("SELECT stage FROM model_routes WHERE owner_id IS NULL"))
    }
    per_user = conn.execute(
        sa.text(
            "SELECT r.id, r.stage FROM model_routes r "
            "LEFT JOIN users u ON u.id = r.owner_id "
            "WHERE r.owner_id IS NOT NULL "
            "ORDER BY CASE WHEN u.email = :local THEN 0 ELSE 1 END, r.created_at, r.id"
        ),
        {"local": LOCAL_USER_EMAIL},
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
    _merge_routes(conn)
    _merge_providers(conn)
    with op.batch_alter_table("acp_agents") as batch:
        batch.drop_column("shared")


def downgrade() -> None:
    with op.batch_alter_table("acp_agents") as batch:
        batch.add_column(
            sa.Column("shared", sa.Boolean(), nullable=False, server_default=sa.false())
        )
