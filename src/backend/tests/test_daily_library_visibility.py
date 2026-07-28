"""每日论文页看得见「哪个库收了它」（#218）+ 实验室工作台的同步收录状态。

抓取成功但一篇都没收进来，和抓取压根没触发同步，在界面上原本长得一模一样——
这两组接口就是为了把这层黑箱打开。
"""

import datetime as dt
import uuid

from app.core.db import get_sessionmaker
from app.models.daily_feed import DailyFeedEntry
from app.models.voyage import VoyageRun, VoyageStep
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


async def test_daily_ingest_status_reports_the_funnel(client):
    _pid, headers, _c, _p, library_id = await _setup(client)

    # 造一轮今天的同步任务及其三个步骤的观测
    async with get_sessionmaker()() as session:
        run = VoyageRun(
            kind="wiki_ingest",
            mode="pipeline",
            status="done",
            goal="daily ingest",
            library_id=library_id,
        )
        session.add(run)
        await session.flush()
        session.add_all(
            [
                VoyageStep(
                    run_id=run.id,
                    seq=1,
                    title="候选",
                    action="wiki.search_candidates",
                    status="passed",
                    observation={"feed_total": 435, "inserted": 12},
                ),
                VoyageStep(
                    run_id=run.id,
                    seq=2,
                    title="打分",
                    action="wiki.score_relevance",
                    status="passed",
                    observation={"succeeded": 12, "excluded": 4},
                ),
                VoyageStep(
                    run_id=run.id,
                    seq=3,
                    title="编译",
                    action="wiki.compile",
                    status="passed",
                    observation={"succeeded": 5},
                ),
            ]
        )
        await session.commit()

    rows = (await client.get("/api/lab/daily-ingest", headers=headers)).json()
    mine = [r for r in rows if r["library_id"] == str(library_id)]
    assert len(mine) == 1, rows
    row = mine[0]
    assert row["status"] == "done"
    assert row["feed_total"] == 435
    assert row["candidates"] == 12
    assert row["admitted"] == 8  # 12 打分通过 - 4 淘汰
    assert row["rejected"] == 4
    assert row["compiled"] == 5
    assert row["step_status"]["candidates"] == "passed"


async def test_daily_ingest_status_hides_other_peoples_libraries(client):
    """只列请求者看得见的库。

    课题自带的隐式库是公共的（全员可读），所以这里专门建一个**个人库**——
    个人库只有创建者和管理员看得见，别人不该在这张状态表里看到它的同步情况。
    """
    _pid, headers, _c, _p, _lib = await _setup(client)
    created = await client.post(
        "/api/libraries",
        # statement 是顶层字段，不是 definition 里的——之前写错了位置，
        # 因为当时它可空所以静默通过，建出来的库其实没有描述
        json={"name": "private-lib", "statement": "A private direction on secret topics."},
        headers=headers,
    )
    assert created.status_code in (200, 201), created.text
    private_id = uuid.UUID(created.json()["id"])

    async with get_sessionmaker()() as session:
        session.add(
            VoyageRun(
                kind="wiki_ingest",
                mode="pipeline",
                status="done",
                goal="x",
                library_id=private_id,
            )
        )
        await session.commit()

    owner_rows = (await client.get("/api/lab/daily-ingest", headers=headers)).json()
    assert any(r["library_id"] == str(private_id) for r in owner_rows), owner_rows

    other = await register_and_login(client, email="stranger@example.com")
    rows = (
        await client.get("/api/lab/daily-ingest", headers={"Authorization": f"Bearer {other}"})
    ).json()
    assert all(r["library_id"] != str(private_id) for r in rows)


async def test_daily_ingest_status_is_empty_without_runs(client):
    _pid, headers, _c, _p, _lib = await _setup(client)
    assert (await client.get("/api/lab/daily-ingest", headers=headers)).json() == []


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
