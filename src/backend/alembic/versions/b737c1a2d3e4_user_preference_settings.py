"""用户偏好搬家：system_settings → owner 用户的 users.settings（#737 配置分层）

纯数据迁移，无 schema 变更。把六个「其实是用户偏好」的平台键拷进 owner 用户
（最早注册的活跃用户，与 #722 的 owner 判定同款）的 settings 命名空间键：

    daily_feed_categories      → daily.categories
    daily_feed_sync_time       → daily.sync_time
    daily_feed_retention_days  → daily.retention_days
    library_sync_scope         → daily.sync_scope
    tts_config                 → tts.admin
    affiliation_extraction_mode → affiliations.extraction_mode

- 库里还没有用户（全新部署）就什么都不做——没有可归属的人，读路径的默认值
  本来就够用。
- 旧 system_settings 行**保留不删**：读路径保留一期只读回退（services/
  owner_settings.py），下一期删回退时连旧行一起清。
- 新键已存在（重复跑/手工迁移过）不覆盖：users.settings 里的值更新。
- downgrade：从所有用户的 settings 剔除这批命名空间键（旧行还在，回退无损；
  升级后新写入的值会丢，属可接受的有损回退，与本仓其他数据迁移同口径）。

Revision ID: b737c1a2d3e4
Revises: d2dcfc8b899f
Create Date: 2026-09-08
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "b737c1a2d3e4"
# 注：cl3（models/迁移）与 cl4（router/stage）并行在途，出货时按合并顺序重挂此指针。
down_revision: str | None = "d2dcfc8b899f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSON_VARIANT = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")

#: 旧平台键 → users.settings 命名空间键
LEGACY_TO_USER = {
    "daily_feed_categories": "daily.categories",
    "daily_feed_sync_time": "daily.sync_time",
    "daily_feed_retention_days": "daily.retention_days",
    "library_sync_scope": "daily.sync_scope",
    "tts_config": "tts.admin",
    "affiliation_extraction_mode": "affiliations.extraction_mode",
}

users_t = sa.table(
    "users",
    sa.column("id"),
    sa.column("is_active"),
    sa.column("created_at"),
    sa.column("settings", JSON_VARIANT),
)

settings_t = sa.table(
    "system_settings",
    sa.column("key", sa.String()),
    sa.column("value", JSON_VARIANT),
)


def upgrade() -> None:
    bind = op.get_bind()
    # owner = 最早注册的活跃用户；created_at 可能并列，按 id 决出稳定次序
    owner = bind.execute(
        sa.select(users_t.c.id, users_t.c.settings)
        .where(users_t.c.is_active == sa.true())
        .order_by(users_t.c.created_at.asc(), users_t.c.id.asc())
        .limit(1)
    ).first()
    if owner is None:
        return

    rows = bind.execute(
        sa.select(settings_t.c.key, settings_t.c.value).where(
            settings_t.c.key.in_(list(LEGACY_TO_USER))
        )
    ).all()
    merged = dict(owner.settings or {})
    changed = False
    for key, value in rows:
        new_key = LEGACY_TO_USER[key]
        if value is None or new_key in merged:
            continue
        merged[new_key] = value
        changed = True
    if changed:
        bind.execute(
            sa.update(users_t).where(users_t.c.id == owner.id).values(settings=merged)
        )


def downgrade() -> None:
    bind = op.get_bind()
    doomed = set(LEGACY_TO_USER.values())
    for row in bind.execute(sa.select(users_t.c.id, users_t.c.settings)).all():
        current = row.settings
        if not isinstance(current, dict):
            continue
        kept = {k: v for k, v in current.items() if k not in doomed}
        if kept != current:
            bind.execute(
                sa.update(users_t)
                .where(users_t.c.id == row.id)
                .values(settings=kept or None)
            )
