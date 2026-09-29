"""drop the legacy system_settings rows behind user preferences

Revision ID: d3f9a1c7e2b4
Revises: b5e2c8d41f7a
Create Date: 2026-09-29

#737 把一批偏好从 system_settings 搬到 owner 用户的 ``users.settings``，读路径当时留了
「新键缺席就回读旧行」的回退，说好只保留一期。它已经跨了好几个版本（#821 E2）。

删回退前先兜底：旧行里还有值、而 owner 身上没有对应新键的，照旧行的值补上——那正是
回退此刻还在读的值，删掉之后行为不变。然后删旧行。没有任何活跃用户的库不动（没有
可以落的地方，旧行也不会再被读到，留着无害）。

每日订阅的旧行 ``daily_feed_categories`` 在 #806 起已不参与读取（订阅按人存，
c1d80a3fb492 用它播过种），一并删掉。

降级是空操作：旧代码读偏好时同样先看 owner.settings，值已经在那里了。
"""

import json

import sqlalchemy as sa

from alembic import op

revision = "d3f9a1c7e2b4"
down_revision = "b5e2c8d41f7a"
branch_labels = None
depends_on = None

#: 旧行 → owner.settings 里的新键
LEGACY_TO_USER_KEY = {
    "daily_feed_sync_time": "daily.sync_time",
    "daily_feed_retention_days": "daily.retention_days",
    "library_sync_scope": "daily.sync_scope",
    "tts_config": "tts.admin",
    "affiliation_extraction_mode": "affiliations.extraction_mode",
}
#: 只删不搬的旧行
OBSOLETE_ROWS = ("daily_feed_categories",)


def _as_value(raw):
    """JSON 列在 sqlite 上回来是字符串，postgres 上已是解析好的值。"""
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw
    return raw


def upgrade() -> None:
    conn = op.get_bind()
    owner = conn.execute(
        sa.text(
            "SELECT id, settings FROM users WHERE is_active = true "
            "ORDER BY created_at ASC, id ASC LIMIT 1"
        )
    ).first()
    if owner is None:
        return
    settings = _as_value(owner.settings)
    settings = dict(settings) if isinstance(settings, dict) else {}
    changed = False
    for legacy_key, user_key in LEGACY_TO_USER_KEY.items():
        row = conn.execute(
            sa.text("SELECT value FROM system_settings WHERE key = :k"), {"k": legacy_key}
        ).first()
        if row is not None and user_key not in settings and row.value is not None:
            settings[user_key] = _as_value(row.value)
            changed = True
    if changed:
        conn.execute(
            sa.text("UPDATE users SET settings = :s WHERE id = :i"),
            {"s": json.dumps(settings), "i": owner.id},
        )
    for key in (*LEGACY_TO_USER_KEY, *OBSOLETE_ROWS):
        conn.execute(sa.text("DELETE FROM system_settings WHERE key = :k"), {"k": key})


def downgrade() -> None:
    pass
