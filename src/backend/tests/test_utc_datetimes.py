"""时间一律带 UTC 时区离开数据库（#872）。

SQLite 没有带时区的时间类型，读出来是 naive 值；序列化成 JSON 不带偏移，前端按本地
时间解析，东八区的用户看到的时间整整差 8 小时（刚建的任务显示「8 小时前」）。
这里钉住三件事：模型读回来带 UTC、接口返回的时间带偏移、比较与排序照旧能用。
"""

import re
import uuid
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

from alembic.config import Config
from sqlalchemy import create_engine, select, text, update
from sqlalchemy.orm import Session

from alembic import command
from app.core.db import get_sessionmaker
from app.models.conversation import Conversation
from app.models.system_setting import SystemSetting
from app.models.voyage import VoyageRun
from tests.conftest import add_paper, register_and_login

BACKEND_DIR = Path(__file__).resolve().parent.parent
KNOWN = datetime(2026, 10, 10, 2, 53, 51, 123456, tzinfo=UTC)
_OFFSET = re.compile(r"(Z|\+00:00)$")


def _assert_utc_timestamps(payload: dict, *, required: tuple[str, ...]) -> None:
    """响应里每个 *_at 字符串都带 UTC 偏移；required 里的字段必须出现。"""
    for key in required:
        assert isinstance(payload.get(key), str), f"{key} missing: {payload.get(key)!r}"
    for key, value in payload.items():
        if key.endswith("_at") and isinstance(value, str):
            assert _OFFSET.search(value), f"{key}={value!r} has no UTC offset"


def test_every_datetime_column_uses_the_utc_type():
    """新加的时间列忘了用 UTCDateTime，就会悄悄退回 naive——这里按全量元数据拦住。"""
    from sqlalchemy import DateTime

    import app.models.base as base
    from app.core.db import Base

    utc_type = getattr(base, "UTCDateTime", None)
    assert utc_type is not None, "app.models.base.UTCDateTime is missing"
    datetime_columns = [
        (f"{table.name}.{column.name}", column.type)
        for table in Base.metadata.sorted_tables
        for column in table.columns
        if isinstance(column.type, DateTime | utc_type)
    ]
    assert len(datetime_columns) > 50
    assert [name for name, kind in datetime_columns if not isinstance(kind, utc_type)] == []


async def test_model_round_trip_returns_aware_utc(app):
    """写入什么时区都换算成 UTC 落库；换一个 session 读回来带 UTC 时区。"""
    east8 = timezone(timedelta(hours=8))
    async with get_sessionmaker()() as session:
        session.add(SystemSetting(key="default-stamp", value=1))
        session.add(
            SystemSetting(
                key="east8-stamp",
                value=2,
                created_at=KNOWN.astimezone(east8),
                updated_at=KNOWN.astimezone(east8),
            )
        )
        await session.commit()

    async with get_sessionmaker()() as session:
        default_row = await session.get(SystemSetting, "default-stamp")
        east8_row = await session.get(SystemSetting, "east8-stamp")
        assert default_row.created_at.tzinfo is UTC
        assert default_row.updated_at.tzinfo is UTC
        assert abs(datetime.now(UTC) - default_row.created_at) < timedelta(minutes=5)
        assert east8_row.created_at.tzinfo is UTC
        assert east8_row.created_at == KNOWN
        assert east8_row.created_at.isoformat() == "2026-10-10T02:53:51.123456+00:00"
        # 库里存的是 UTC 钟面时间（不是 +08:00 的钟面数字）
        stored = (
            await session.execute(
                text("SELECT created_at FROM system_settings WHERE key = 'east8-stamp'")
            )
        ).scalar_one()
        assert str(stored).startswith("2026-10-10 02:53:51")

        # onupdate 也走同一类型
        east8_row.value = 3
        await session.commit()
    async with get_sessionmaker()() as session:
        row = await session.get(SystemSetting, "east8-stamp")
        assert row.updated_at.tzinfo is UTC
        assert row.updated_at > KNOWN


async def test_comparisons_ordering_and_ranges_still_work(app):
    """<、>、between、排序、聚合：带时区的边界值（含非 UTC 的）和库里的值比较照旧。"""
    from sqlalchemy import func

    east8 = timezone(timedelta(hours=8))
    async with get_sessionmaker()() as session:
        for index in range(4):
            stamp = KNOWN + timedelta(hours=index)
            session.add(
                SystemSetting(key=f"k{index}", value=index, created_at=stamp, updated_at=stamp)
            )
        await session.commit()

    async with get_sessionmaker()() as session:

        async def keys(*where) -> list[str]:
            stmt = select(SystemSetting.key).where(*where).order_by(SystemSetting.created_at)
            return list((await session.execute(stmt)).scalars())

        cut = KNOWN + timedelta(hours=1, minutes=30)
        assert await keys(SystemSetting.created_at < cut) == ["k0", "k1"]
        assert await keys(SystemSetting.created_at > cut) == ["k2", "k3"]
        # 同一时刻用 +08:00 表达，结果必须一样（以前按钟面数字比，差 8 小时）
        assert await keys(SystemSetting.created_at > cut.astimezone(east8)) == ["k2", "k3"]
        assert await keys(
            SystemSetting.created_at.between(KNOWN + timedelta(hours=1), KNOWN + timedelta(hours=2))
        ) == ["k1", "k2"]
        assert await keys(SystemSetting.created_at == KNOWN.astimezone(east8)) == ["k0"]
        newest = (
            await session.execute(
                select(SystemSetting.key).order_by(SystemSetting.created_at.desc()).limit(1)
            )
        ).scalar_one()
        assert newest == "k3"
        latest = (await session.execute(select(func.max(SystemSetting.created_at)))).scalar_one()
        assert latest == KNOWN + timedelta(hours=3)
        assert latest.tzinfo is UTC
        # 读出来的值能直接和 utcnow() 相减（naive 值在这里会抛 TypeError）
        row = await session.get(SystemSetting, "k0")
        assert datetime.now(UTC) - row.created_at > timedelta(0)


def test_rows_in_an_alembic_migrated_sqlite_file_read_back_aware(tmp_path):
    """不需要数据迁移：迁移建出来的库里，旧代码写下的行（UTC 钟面时间、无偏移）读回来
    带 UTC，和新写入的行一起比较、排序都对。"""
    db_path = tmp_path / "migrated.db"
    cfg = Config()
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{db_path}")
    command.upgrade(cfg, "head")

    engine = create_engine(f"sqlite:///{db_path}")
    try:
        with engine.begin() as conn:
            # 旧代码落库的样子：两种精度都出现过
            conn.execute(
                text(
                    "INSERT INTO system_settings (key, value, created_at, updated_at) VALUES "
                    "('old-a', '1', '2026-10-10 02:53:51.123456', '2026-10-10 02:53:51.123456'),"
                    "('old-b', '2', '2026-10-10 04:00:00', '2026-10-10 04:00:00')"
                )
            )
        with Session(engine) as session:
            later = KNOWN + timedelta(hours=3)
            session.add(SystemSetting(key="new-c", value=3, created_at=later, updated_at=later))
            session.commit()
        with Session(engine) as session:
            rows = session.execute(
                select(SystemSetting).order_by(SystemSetting.created_at)
            ).scalars()
            got = [(row.key, row.created_at) for row in rows]
            assert got == [
                ("old-a", KNOWN),
                ("old-b", datetime(2026, 10, 10, 4, 0, tzinfo=UTC)),
                ("new-c", KNOWN + timedelta(hours=3)),
            ]
            assert all(stamp.tzinfo is UTC for _, stamp in got)
            between = session.execute(
                select(SystemSetting.key).where(
                    SystemSetting.created_at > KNOWN,
                    SystemSetting.created_at < KNOWN + timedelta(hours=3),
                )
            ).scalars()
            assert list(between) == ["old-b"]
    finally:
        engine.dispose()


async def test_task_api_timestamps_carry_the_utc_offset(client, queue_stub):
    headers = {"Authorization": f"Bearer {await register_and_login(client)}"}
    project = await client.post("/api/projects", json={"name": "tz"}, headers=headers)
    assert project.status_code == 201, project.text
    created = await client.post(
        "/api/voyages",
        json={"kind": "demo", "project_id": project.json()["id"], "goal": "时间"},
        headers=headers,
    )
    assert created.status_code == 201, created.text
    voyage_id = created.json()["id"]
    async with get_sessionmaker()() as session:
        await session.execute(
            update(VoyageRun).where(VoyageRun.id == uuid.UUID(voyage_id)).values(created_at=KNOWN)
        )
        await session.commit()

    detail = (await client.get(f"/api/voyages/{voyage_id}", headers=headers)).json()
    _assert_utc_timestamps(detail, required=("created_at", "updated_at"))
    assert datetime.fromisoformat(detail["created_at"]) == KNOWN
    assert detail["created_at"].startswith("2026-10-10T02:53:51.123456")
    listed = (await client.get("/api/voyages", headers=headers)).json()
    _assert_utc_timestamps(listed[0], required=("created_at", "updated_at"))
    # 课题本身也一样
    projects = (await client.get("/api/projects", headers=headers)).json()
    _assert_utc_timestamps(projects[0], required=("created_at",))


async def test_paper_api_timestamps_carry_the_utc_offset(client):
    headers = {"Authorization": f"Bearer {await register_and_login(client)}"}
    project = await client.post("/api/projects", json={"name": "tz-papers"}, headers=headers)
    project_id = project.json()["id"]
    async with get_sessionmaker()() as session:
        paper = await add_paper(
            session,
            project_id=project_id,
            title="Timed paper",
            status="compiled",
            wiki_content="# 解读",
            compiled_at=KNOWN,
        )
        await session.commit()
        paper_id = str(paper.id)

    detail = await client.get(f"/api/projects/{project_id}/papers/{paper_id}", headers=headers)
    assert detail.status_code == 200, detail.text
    _assert_utc_timestamps(detail.json(), required=("created_at", "compiled_at"))
    assert datetime.fromisoformat(detail.json()["compiled_at"]) == KNOWN
    pooled = await client.get(f"/api/papers/{paper_id}", headers=headers)
    assert pooled.status_code == 200, pooled.text
    _assert_utc_timestamps(pooled.json(), required=("created_at", "compiled_at"))


async def test_conversation_api_timestamps_carry_the_utc_offset(client):
    headers = {"Authorization": f"Bearer {await register_and_login(client)}"}
    created = await client.post("/api/chat/conversations", json={}, headers=headers)
    assert created.status_code == 201, created.text
    conversation_id = created.json()["id"]
    async with get_sessionmaker()() as session:
        await session.execute(
            update(Conversation)
            .where(Conversation.id == uuid.UUID(conversation_id))
            .values(last_message_at=KNOWN)
        )
        await session.commit()

    listed = (await client.get("/api/chat/conversations", headers=headers)).json()
    row = next(item for item in listed if item["id"] == conversation_id)
    _assert_utc_timestamps(row, required=("last_message_at",))
    assert datetime.fromisoformat(row["last_message_at"]) == KNOWN
