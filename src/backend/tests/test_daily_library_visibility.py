"""每日论文页看得见「哪个库收了它」（#218）与库列表的收录口径。

（原先同文件还测 /lab/daily-ingest 的同步漏斗——lab 数据面板随 #626 移除，
那组接口与用例一并退役。）
"""

import datetime as dt
import uuid

from app.core.db import get_sessionmaker
from app.models.daily_feed import DailyFeedEntry
from tests.conftest import add_paper, register_and_login


def _today() -> dt.date:
    return dt.datetime.now(dt.UTC).date()


async def _setup(client):
    token = await register_and_login(client)
    headers = {"Authorization": f"Bearer {token}"}
    resp = await client.post(
        "/api/projects",
        json={"name": "daily-vis", "statement": "agent planning"},
        headers=headers,
    )
    project_id = uuid.UUID(resp.json()["id"])

    async with get_sessionmaker()() as session:
        collected = await add_paper(
            session, project_id=project_id, title="Collected One", status="compiled"
        )
        passed_over = await add_paper(
            session, project_id=project_id, title="Passed Over", status="candidate"
        )
        session.add_all([collected, passed_over])
        await session.flush()
        # 两篇都在每日池里；只有一篇真正被库收录
        for paper in (collected, passed_over):
            session.add(
                DailyFeedEntry(paper_id=paper.id, feed_date=_today(), primary_category="cs.AI")
            )
        library_id = collected.id  # 占位，下面用真实库 id 覆盖
        await session.commit()
        collected_id, passed_id = collected.id, passed_over.id

    libs = (await client.get("/api/libraries", headers=headers)).json()
    library_id = libs[0]["id"] if isinstance(libs, list) else libs["items"][0]["id"]
    return project_id, headers, collected_id, passed_id, uuid.UUID(library_id)


async def test_daily_list_can_filter_by_collecting_library(client):
    _pid, headers, collected_id, passed_id, library_id = await _setup(client)

    everything = (await client.get("/api/daily/papers?size=50", headers=headers)).json()
    ids = {row["paper_id"] for row in everything["items"]}
    assert {str(collected_id), str(passed_id)} <= ids

    scoped = (
        await client.get(f"/api/daily/papers?size=50&library_id={library_id}", headers=headers)
    ).json()
    scoped_ids = {row["paper_id"] for row in scoped["items"]}
    assert str(collected_id) in scoped_ids
    # 只是候选、还没被收录的不算「这个库收了它」
    assert str(passed_id) not in scoped_ids
    assert scoped["total"] == len(scoped_ids)


async def test_daily_list_unknown_library_returns_nothing(client):
    _pid, headers, _c, _p, _lib = await _setup(client)
    resp = await client.get(f"/api/daily/papers?library_id={uuid.uuid4()}", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["items"] == []


async def test_library_list_can_show_only_todays_daily_collections(client):
    """文献库列表的「今日新收录」：今天入库 + 来自每日论文池，两个条件都要满足。"""
    import datetime as dt

    from sqlalchemy import select as sa_select

    from app.models.library_direction import LibraryPaper

    project_id, headers, collected_id, _passed_id, _lib = await _setup(client)

    async with get_sessionmaker()() as session:
        # 一篇手工加的（不在每日池里），今天入库——不该出现在「今日新收录」里
        manual = await add_paper(
            session, project_id=project_id, title="Manually Added", status="compiled"
        )
        session.add(manual)
        await session.flush()
        manual_id = manual.id
        # 一篇来自每日池但是昨天入库的——也不该出现
        old = await add_paper(
            session, project_id=project_id, title="Collected Yesterday", status="compiled"
        )
        session.add(old)
        await session.flush()
        session.add(DailyFeedEntry(paper_id=old.id, feed_date=_today(), primary_category="cs.AI"))
        await session.flush()
        yesterday = dt.datetime.now(dt.UTC) - dt.timedelta(days=1)
        membership = (
            await session.execute(sa_select(LibraryPaper).where(LibraryPaper.paper_id == old.id))
        ).scalar_one()
        membership.created_at = yesterday
        old_id = old.id
        await session.commit()

    since = (dt.datetime.now(dt.UTC) - dt.timedelta(hours=1)).isoformat()
    resp = await client.get(
        f"/api/projects/{project_id}/papers",
        # 走 params 而不是拼串：ISO 时间里的 + 号在 URL 里会被当成空格
        params={"status": "library", "daily_only": "true", "created_from": since},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    ids = {row["id"] for row in resp.json()["items"]}
    assert str(collected_id) in ids
    assert str(manual_id) not in ids, "手工加的不算「从每日论文自动收录」"
    assert str(old_id) not in ids, "昨天入库的不算今天新收录"


async def test_latest_batch_view_follows_the_last_sync_not_the_calendar(client):
    """「最新收录」按上次同步新增算，不是按「今天入库」。

    按日历卡的话，上次同步在昨天就永远显示 0 篇，而用户想看的是「上次更新带进来什么」。
    """
    import datetime as dt

    from sqlalchemy import select as sa_select

    from app.models.library_direction import LibraryPaper
    from app.models.voyage import VoyageRun

    project_id, headers, collected_id, _passed_id, library_id = await _setup(client)

    async with get_sessionmaker()() as session:
        run = VoyageRun(
            kind="wiki_ingest",
            mode="pipeline",
            status="done",
            goal="上次同步",
            library_id=library_id,
        )
        session.add(run)
        await session.flush()
        two_days_ago = dt.datetime.now(dt.UTC) - dt.timedelta(days=2)
        run.created_at = two_days_ago
        membership = (
            await session.execute(
                sa_select(LibraryPaper).where(LibraryPaper.paper_id == collected_id)
            )
        ).scalar_one()
        membership.created_at = two_days_ago + dt.timedelta(minutes=1)
        # 再放一篇「上次同步之前就在库里」的：它不该算进最新收录
        older = await add_paper(
            session, project_id=project_id, title="Added Long Ago", status="compiled"
        )
        session.add(older)
        await session.flush()
        older_membership = (
            await session.execute(sa_select(LibraryPaper).where(LibraryPaper.paper_id == older.id))
        ).scalar_one()
        older_membership.created_at = two_days_ago - dt.timedelta(days=5)
        older_id = older.id
        await session.commit()

    resp = await client.get(
        f"/api/projects/{project_id}/papers",
        params={"status": "library", "last_sync_only": "true"},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    ids = {row["id"] for row in resp.json()["items"]}
    assert str(collected_id) in ids, "上次同步（两天前）新增的那篇应当算「最新收录」"
    assert str(older_id) not in ids, "上次同步之前就在库里的不该算新增"


async def test_latest_batch_is_empty_when_the_library_never_synced(client):
    """从没同步过的库：0 篇，而不是退化成全部。"""
    project_id, headers, _c, _p, _lib = await _setup(client)
    resp = await client.get(
        f"/api/projects/{project_id}/papers",
        params={"status": "library", "last_sync_only": "true"},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["items"] == []


async def test_candidate_is_not_reported_as_already_in_the_library(client):
    """粗排选中但还没打分的 candidate，不能显示成「已在库中」。

    线上碰到的：VetClaw 那篇刚被 CUA 的向量粗排捞进候选队列（status=candidate、
    还没打分），收录弹窗却标成「已在库中」并把复选框禁用了——想手动收进去都点不了。
    库内的口径是 PAPER_STATUS_GROUPS["library"]，candidate 不在其中。
    """
    from sqlalchemy import select as sa_select

    from app.models.daily_feed import DailyFeedEntry
    from app.models.library_direction import LibraryPaper

    _pid, headers, _collected, passed_id, library_id = await _setup(client)

    async with get_sessionmaker()() as session:
        # passed_id 那篇在 _setup 里就是 candidate
        membership = (
            await session.execute(sa_select(LibraryPaper).where(LibraryPaper.paper_id == passed_id))
        ).scalar_one()
        assert membership.status == "candidate"
        entry_id = (
            await session.execute(
                sa_select(DailyFeedEntry.id).where(DailyFeedEntry.paper_id == passed_id)
            )
        ).scalar_one()

    resp = await client.get(f"/api/daily/papers/{entry_id}/collections", headers=headers)
    assert resp.status_code == 200, resp.text
    assert str(library_id) not in resp.json()["direction_library_ids"], (
        "candidate 被当成了「已在库中」"
    )


async def test_manual_collect_promotes_a_candidate_instead_of_skipping_it(client):
    """人明确勾选「收进这个库」时，已有的 candidate 行要提升为 included。

    否则复选框放开了也没用：ensure_membership 遇到已有行直接返回，状态原封不动，
    勾了确认却什么都没发生。
    """
    from sqlalchemy import select as sa_select

    from app.models.library_direction import LibraryPaper

    _pid, headers, _collected, passed_id, library_id = await _setup(client)

    resp = await client.post(
        "/api/daily/collect",
        json={
            "paper_ids": [str(passed_id)],
            "direction_library_ids": [str(library_id)],
            "topic_ids": [],
            "personal": False,
        },
        headers=headers,
    )
    assert resp.status_code == 200, resp.text

    async with get_sessionmaker()() as session:
        membership = (
            await session.execute(sa_select(LibraryPaper).where(LibraryPaper.paper_id == passed_id))
        ).scalar_one()
        assert membership.status == "included", "手动收录没有把 candidate 提升为 included"
