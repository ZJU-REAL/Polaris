"""per-user daily subscriptions

Revision ID: c1d80a3fb492
Revises: d7f4a16c8e29
Create Date: 2026-09-16

每日订阅此前只存在 owner 的 ``users.settings`` 上（#737 分层时归到那里，理由是
「每日池是全部署共享的一份」）。池子共享是对的，订阅归一个人所有则在有第二个用户
之后就错了——#806。

读路径改成只认本人的键之后，不播这一份种，升级当天除 owner 外每个人的订阅都会变成
空：信息流一篇不剩，而他们没做任何操作。所以把 owner 当时的值原样抄给每个活跃用户。

owner 自己不动（他本来就有这份值，原样保留）。没有用户的库、owner 没配过订阅的库，
都不需要做任何事。

降级把抄过去的键删掉，只删与 owner 当时值完全相同的那些——用户升级后自己改过的，
删了就是把他自己的设置抹掉。
"""

import json

import sqlalchemy as sa

from alembic import op

revision = "c1d80a3fb492"
down_revision = "d7f4a16c8e29"
branch_labels = None
depends_on = None

CATEGORIES_KEY = "daily.categories"
SUBSCRIPTIONS_KEY = "daily.subscriptions"
LEGACY_CATEGORIES_ROW = "daily_feed_categories"


def _owner_row(conn):
    """部署主人 = 最早注册的活跃用户（与 services/owner.py 同一判据）。"""
    return conn.execute(
        sa.text(
            "SELECT id, settings FROM users WHERE is_active = true "
            "ORDER BY created_at ASC, id ASC LIMIT 1"
        )
    ).first()


def _as_dict(value) -> dict:
    """settings 列在 sqlite 上可能回来是字符串，postgres 上是 dict。"""
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _owner_subscription(conn) -> dict:
    """owner 当时生效的订阅（含迁移期的 system_settings 回退行）。"""
    owner = _owner_row(conn)
    seed: dict = {}
    if owner is not None:
        settings = _as_dict(owner.settings)
        for key in (CATEGORIES_KEY, SUBSCRIPTIONS_KEY):
            if key in settings:
                seed[key] = settings[key]
    if CATEGORIES_KEY not in seed:
        # 旧的 system_settings 行：read_setting 当时会回读它，所以它也是「当时生效的值」
        row = conn.execute(
            sa.text("SELECT value FROM system_settings WHERE key = :k"),
            {"k": LEGACY_CATEGORIES_ROW},
        ).first()
        if row is not None and row.value is not None:
            value = row.value
            if isinstance(value, str):
                try:
                    value = json.loads(value)
                except json.JSONDecodeError:
                    value = None
            if isinstance(value, list):
                seed[CATEGORIES_KEY] = value
    return seed


def upgrade() -> None:
    conn = op.get_bind()
    seed = _owner_subscription(conn)
    if not seed:
        return  # owner 没订过任何东西：没有可播的种
    owner = _owner_row(conn)
    owner_id = owner.id if owner is not None else None
    rows = conn.execute(sa.text("SELECT id, settings FROM users")).all()
    for row in rows:
        if owner_id is not None and row.id == owner_id:
            continue  # 主人那份原样留着
        settings = _as_dict(row.settings)
        merged = dict(settings)
        changed = False
        for key, value in seed.items():
            # 已经有自己那份的不覆盖（正常不会发生，但迁移重跑必须幂等）
            if key not in merged:
                merged[key] = value
                changed = True
        if changed:
            conn.execute(
                sa.text("UPDATE users SET settings = :s WHERE id = :i"),
                {"s": json.dumps(merged), "i": row.id},
            )


def downgrade() -> None:
    conn = op.get_bind()
    seed = _owner_subscription(conn)
    if not seed:
        return
    owner = _owner_row(conn)
    owner_id = owner.id if owner is not None else None
    rows = conn.execute(sa.text("SELECT id, settings FROM users")).all()
    for row in rows:
        if owner_id is not None and row.id == owner_id:
            continue
        settings = _as_dict(row.settings)
        merged = dict(settings)
        changed = False
        for key, value in seed.items():
            # 只收回与当时播下去的完全一致的那份；用户后来自己改过的保留，
            # 删掉就是把他自己设的东西一起抹了
            if key in merged and merged[key] == value:
                del merged[key]
                changed = True
        if changed:
            conn.execute(
                sa.text("UPDATE users SET settings = :s WHERE id = :i"),
                {"s": json.dumps(merged), "i": row.id},
            )
