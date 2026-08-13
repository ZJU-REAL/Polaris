"""每日新论文池：同步/过期清理（service 层）+ 浏览/点赞/收录/分类（API 层）。"""

import datetime as dt
import uuid

import pytest

from tests.conftest import make_project_with_library, register_and_login

pytestmark = pytest.mark.asyncio


def _rss_entry(arxiv_id: str, title: str, *, announce: str = "new") -> dict:
    return {
        "arxiv_id": arxiv_id,
        "title": title,
        "abstract": f"Abstract of {title}",
        "authors": [{"name": "Ada Lovelace"}],
        "published": "2026-07-24T00:00:00+00:00",
        "updated": None,
        "year": 2026,
        "categories": ["cs.AI"],
        "primary_category": "cs.AI",
        "doi": None,
        "url": f"https://arxiv.org/abs/{arxiv_id}",
        "pdf_url": f"https://arxiv.org/pdf/{arxiv_id}",
        "announce_type": announce,
    }


class _StubArxiv:
    """按分类返回固定 RSS 条目的假客户端。

    ``batch_date`` 默认为今天：真实 RSS 会声明这批公告是哪天的，抓早了拿到旧批次会被
    判为陈旧并报错。想验证那条路径就显式传一个过去的日期。
    """

    def __init__(self, by_category: dict[str, list[dict]], batch_date=None):
        self.by_category = by_category
        self.batch_date = batch_date

    async def fetch_new(self, category: str):
        import datetime as _dt

        at = _dt.datetime.combine(
            self.batch_date or _dt.datetime.now(_dt.UTC).date(), _dt.time(), tzinfo=_dt.UTC
        )
        return self.by_category.get(category, []), at


async def _run_sync(monkeypatch, by_category: dict[str, list[dict]]) -> dict:
    from app.core.db import get_sessionmaker
    from app.services import daily_feed

    monkeypatch.setattr(daily_feed, "get_arxiv_client", lambda: _StubArxiv(by_category))
    async with get_sessionmaker()() as session:
        return await daily_feed.sync_daily_feed(session)


async def test_sync_idempotent_and_cross_merge(client, monkeypatch):
    await register_and_login(client)
    feed = {
        "cs.AI": [_rss_entry("2607.00001", "Paper A"), _rss_entry("2607.00002", "Paper B")],
        # 同一篇论文在另一分类以 cross 出现 → 合并 categories，不重复建行
        "cs.CL": [_rss_entry("2607.00001", "Paper A", announce="cross")],
        "cs.CV": [],
    }
    result = await _run_sync(monkeypatch, feed)
    assert result["fetched"] == 3
    assert result["created"] == 2

    # 同日重跑幂等
    result2 = await _run_sync(monkeypatch, feed)
    assert result2["created"] == 0

    from sqlalchemy import select

    from app.core.db import get_sessionmaker
    from app.models.daily_feed import DailyFeedEntry

    async with get_sessionmaker()() as session:
        rows = (await session.execute(select(DailyFeedEntry))).scalars().all()
        assert len(rows) == 2
        merged = next(r for r in rows if "cs.CL" in (r.categories or []))
        assert set(merged.categories) == {"cs.AI", "cs.CL"}

    # 高级过滤：分类命中合并进 categories 的条目；announce 过滤
    token = await register_and_login(client, email="filter@example.com")
    fh = {"Authorization": f"Bearer {token}"}
    resp = await client.get("/api/daily/papers", params={"category": "cs.CL"}, headers=fh)
    assert resp.status_code == 200 and resp.json()["total"] == 1
    assert resp.json()["items"][0]["title"] == "Paper A"
    resp = await client.get("/api/daily/papers", params={"announce": "new"}, headers=fh)
    assert resp.json()["total"] == 2  # Paper A 首见于 cs.AI 时 announce=new
    resp = await client.get("/api/daily/papers", params={"announce": "cross"}, headers=fh)
    assert resp.json()["total"] == 0


async def test_cleanup_expired_keeps_paper_and_membership(client, monkeypatch):
    token = await register_and_login(client)
    headers = {"Authorization": f"Bearer {token}"}
    await _run_sync(monkeypatch, {"cs.AI": [_rss_entry("2607.00010", "Old Paper")]})

    resp = await client.get("/api/daily/papers", headers=headers)
    assert resp.status_code == 200
    item = resp.json()["items"][0]
    entry_id, paper_id = item["entry_id"], item["paper_id"]

    # 点个赞 + 收进个人库
    resp = await client.put(f"/api/daily/papers/{entry_id}/like", headers=headers)
    assert resp.status_code == 200
    resp = await client.post(
        "/api/daily/collect", json={"paper_ids": [paper_id], "personal": True}, headers=headers
    )
    assert resp.status_code == 200

    # 把 entry 改成 8 天前 → 再同步 → entry 与赞消失，Paper 与个人库条目仍在
    from sqlalchemy import select

    from app.core.db import get_sessionmaker
    from app.models.daily_feed import DailyFeedEntry, DailyFeedLike
    from app.models.library import UserLibraryEntry
    from app.models.paper import Paper

    async with get_sessionmaker()() as session:
        entry = await session.get(DailyFeedEntry, uuid.UUID(entry_id))
        # 比保留期更早才算过期（保留期默认 14 天，且管理员可改）
        from app.services.daily_feed import DEFAULT_RETENTION_DAYS

        entry.feed_date = entry.feed_date - dt.timedelta(days=DEFAULT_RETENTION_DAYS + 1)
        await session.commit()

    result = await _run_sync(monkeypatch, {"cs.AI": []})
    assert result["expired"] == 1

    async with get_sessionmaker()() as session:
        assert await session.get(DailyFeedEntry, uuid.UUID(entry_id)) is None
        likes = (await session.execute(select(DailyFeedLike))).scalars().all()
        assert likes == []
        assert await session.get(Paper, uuid.UUID(paper_id)) is not None
        saved = (await session.execute(select(UserLibraryEntry))).scalars().all()
        assert len(saved) == 1 and saved[0].saved

    # 「我赞过的」也随之清空
    resp = await client.get("/api/daily/liked", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["total"] == 0


async def test_list_filters_by_author_and_affiliation(client, monkeypatch):
    """详情面板里点作者 / 点机构 chip 带上来的过滤（JSON 文本包含匹配）。

    机构是编译解读时才解析出来的，同步进来的池论文没有——这里直接写进论文行模拟已编译。
    """
    from sqlalchemy import select

    from app.core.db import get_sessionmaker
    from app.models.paper import Paper

    token = await register_and_login(client)
    headers = {"Authorization": f"Bearer {token}"}
    await _run_sync(
        monkeypatch,
        {
            "cs.AI": [
                _rss_entry("2607.00041", "Vision Paper"),
                _rss_entry("2607.00042", "Language Paper"),
            ]
        },
    )
    # 造作者/机构：Vision=Kaiming He@Meta AI，Language=Jacob Devlin@Google Research
    async with get_sessionmaker()() as session:
        papers = (await session.execute(select(Paper))).scalars().all()
        for paper in papers:
            if paper.title == "Vision Paper":
                paper.authors = [{"name": "Kaiming He"}]
                paper.affiliations = ["Meta AI"]
            else:
                paper.authors = [{"name": "Jacob Devlin"}]
                paper.affiliations = ["Google Research"]
        await session.commit()

    async def query(**params):
        qs = "&".join(f"{k}={v}" for k, v in params.items())
        resp = await client.get(f"/api/daily/papers?{qs}", headers=headers)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        return [i["title"] for i in body["items"]], body["total"]

    got, total = await query()
    assert total == 2
    # 机构随列表一起返回，前端详情才能画出可点的 chips
    resp = await client.get("/api/daily/papers", headers=headers)
    by_title = {i["title"]: i for i in resp.json()["items"]}
    assert by_title["Vision Paper"]["affiliations"] == ["Meta AI"]

    got, total = await query(author="Kaiming")
    assert got == ["Vision Paper"] and total == 1
    got, total = await query(affiliation="Google")
    assert got == ["Language Paper"] and total == 1
    got, total = await query(author="Nobody")
    assert got == [] and total == 0
    # 与其他条件叠加
    got, _ = await query(affiliation="Meta", announce="new")
    assert got == ["Vision Paper"]


async def test_like_toggle_facepile_and_sort(client, monkeypatch):
    token_a = await register_and_login(client)  # 首个 = admin
    token_b = await register_and_login(client, email="bob@example.com")
    ha = {"Authorization": f"Bearer {token_a}"}
    hb = {"Authorization": f"Bearer {token_b}"}
    await _run_sync(
        monkeypatch,
        {"cs.AI": [_rss_entry("2607.00021", "Hot Paper"), _rss_entry("2607.00022", "Cold Paper")]},
    )
    resp = await client.get("/api/daily/papers", headers=ha)
    hot = next(i for i in resp.json()["items"] if i["title"] == "Hot Paper")
    eid = hot["entry_id"]

    # 双人点赞；重复点幂等
    r1 = await client.put(f"/api/daily/papers/{eid}/like", headers=ha)
    assert r1.json()["like_count"] == 1 and r1.json()["liked_by_me"] is True
    await client.put(f"/api/daily/papers/{eid}/like", headers=hb)
    r2 = await client.put(f"/api/daily/papers/{eid}/like", headers=hb)
    assert r2.json()["like_count"] == 2

    # facepile：本人永远排最前（display_name 夹具里都叫 Alice，按 id 断言）
    preview = r2.json()["likers_preview"]
    assert len(preview) == 2
    me = await client.get("/api/users/me", headers=hb)
    assert preview[0]["id"] == me.json()["id"]

    # 默认按赞数排序：Hot 在前
    resp = await client.get("/api/daily/papers", headers=ha)
    assert resp.json()["items"][0]["title"] == "Hot Paper"
    assert resp.json()["items"][0]["like_count"] == 2

    # 完整名单
    resp = await client.get(f"/api/daily/papers/{eid}/likers", headers=ha)
    assert resp.status_code == 200 and len(resp.json()) == 2

    # 取消赞
    r3 = await client.delete(f"/api/daily/papers/{eid}/like", headers=hb)
    assert r3.json()["like_count"] == 1 and r3.json()["liked_by_me"] is False
    resp = await client.get("/api/daily/liked", headers=hb)
    assert resp.json()["total"] == 0
    resp = await client.get("/api/daily/liked", headers=ha)
    assert resp.json()["total"] == 1


async def test_collect_to_library_topic_personal(client, monkeypatch):
    token = await register_and_login(client)
    headers = {"Authorization": f"Bearer {token}"}
    project_id, library_id = await make_project_with_library(client, headers, name="daily-proj")
    await _run_sync(monkeypatch, {"cs.AI": [_rss_entry("2607.00031", "Collect Me")]})
    resp = await client.get("/api/daily/papers", headers=headers)
    item = resp.json()["items"][0]

    payload = {
        "paper_ids": [item["paper_id"]],
        "direction_library_ids": [str(library_id)],
        "topic_ids": [project_id],
        "personal": True,
    }
    # 收录会启动与手动添加同款的后台补全（#74），打分目标 = 第一个成功收录的方向库
    launched: list[dict] = []

    async def _fake_launch(**kwargs):
        launched.append(kwargs)
        return "task-stub"

    from app.services import paper_enrich

    monkeypatch.setattr(paper_enrich, "launch_paper_enrichment", _fake_launch)

    resp = await client.post("/api/daily/collect", json=payload, headers=headers)
    assert resp.status_code == 200
    results = {r["target_type"]: r for r in resp.json()["results"]}
    assert results["library"]["added"] == 1 and not results["library"]["forbidden"]
    assert results["topic"]["added"] == 1
    # 入架必入个人库（add_to_shelf 自带同步），个人库目标看到的是「已存在」
    assert results["personal"]["added"] + results["personal"]["skipped_existing"] == 1

    # 池论文是轻量行（无 PDF）→ 必然触发补全，且目标库/课题归因正确
    assert len(launched) == 1
    assert str(launched[0]["paper_id"]) == item["paper_id"]
    assert launched[0]["library_id"] == library_id
    assert str(launched[0]["project_id"]) == project_id
    # 响应回传补全任务，前端据此弹与手动添加同款的分阶段进度框
    tasks = resp.json()["tasks"]
    assert len(tasks) == 1
    assert tasks[0]["task_id"] == "task-stub"
    assert tasks[0]["paper_id"] == item["paper_id"]

    # 重复收录 → skipped_existing
    resp = await client.post("/api/daily/collect", json=payload, headers=headers)
    results = {r["target_type"]: r for r in resp.json()["results"]}
    assert results["library"]["skipped_existing"] == 1
    assert results["topic"]["skipped_existing"] == 1
    assert results["personal"]["skipped_existing"] == 1

    # 成员行 status=included
    from sqlalchemy import select

    from app.core.db import get_sessionmaker
    from app.models.library_direction import LibraryPaper

    async with get_sessionmaker()() as session:
        membership = (
            await session.execute(select(LibraryPaper).where(LibraryPaper.library_id == library_id))
        ).scalar_one()
        assert membership.status == "included"

    # collections 预勾选口径
    resp = await client.get(f"/api/daily/papers/{item['entry_id']}/collections", headers=headers)
    data = resp.json()
    assert str(library_id) in data["direction_library_ids"]
    assert project_id in data["topic_ids"]
    assert data["in_personal"] is True

    # 非成员课题 → forbidden，不整体失败
    other = await register_and_login(client, email="eve@example.com")
    oh = {"Authorization": f"Bearer {other}"}
    resp = await client.post(
        "/api/daily/collect",
        json={"paper_ids": [item["paper_id"]], "topic_ids": [project_id]},
        headers=oh,
    )
    assert resp.status_code == 200
    assert resp.json()["results"][0]["forbidden"] is True


async def test_compile_entry_and_collect(client, monkeypatch):
    """单篇解读编译（fake LLM）落 paper_wikis；收录进方向库 / 书架 / 个人库后照样读得到。"""
    token = await register_and_login(client)
    headers = {"Authorization": f"Bearer {token}"}
    project_id, library_id = await make_project_with_library(client, headers, name="wiki-proj")
    await _run_sync(monkeypatch, {"cs.AI": [_rss_entry("2607.00041", "Compile Me")]})
    resp = await client.get("/api/daily/papers", headers=headers)
    item = resp.json()["items"][0]
    entry_id, paper_id = item["entry_id"], item["paper_id"]
    assert item["has_wiki"] is False

    resp = await client.post(f"/api/daily/papers/{entry_id}/compile", headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["wiki_content"].strip()

    resp = await client.get(f"/api/daily/papers/{entry_id}", headers=headers)
    assert resp.json()["has_wiki"] is True and resp.json()["wiki_content"].strip()

    resp = await client.post(
        "/api/daily/collect",
        json={
            "paper_ids": [paper_id],
            "direction_library_ids": [str(library_id)],
            "topic_ids": [project_id],
            "personal": True,
        },
        headers=headers,
    )
    assert resp.status_code == 200

    from sqlalchemy import select

    from app.core.db import get_sessionmaker
    from app.models.library import UserLibraryEntry
    from app.models.library_direction import LibraryPaper
    from app.models.topic_shelf import TopicPaper

    async with get_sessionmaker()() as session:
        membership = (
            await session.execute(select(LibraryPaper).where(LibraryPaper.library_id == library_id))
        ).scalar_one()
        assert membership.status == "included"
        shelf_row = (await session.execute(select(TopicPaper))).scalar_one()
        assert shelf_row.paper_id == uuid.UUID(paper_id)
        personal = (await session.execute(select(UserLibraryEntry))).scalar_one()
        assert personal.saved is True

    # 解读只有一份：库 / 书架 / 个人库三条路径读到的都是它
    resp = await client.get(f"/api/papers/{paper_id}", headers=headers)
    wiki = resp.json()["wiki_content"]
    assert wiki and wiki.strip()
    resp = await client.get(f"/api/projects/{project_id}/shelf", headers=headers)
    assert resp.json()["items"][0]["wiki_content"] == wiki
    entry_resp = await client.get("/api/me/library?tab=saved", headers=headers)
    lib_entry_id = entry_resp.json()["items"][0]["id"]
    resp = await client.get(f"/api/me/library/{lib_entry_id}", headers=headers)
    assert resp.json()["wiki_content"] == wiki


async def test_compile_entry_links_concepts_without_library(client, monkeypatch):
    """每日推送编译也要上链概念：论文不属于任何库照建词条，费用记平台级（library_id 空）。

    编译两篇：概念要被 2 篇论文提到才转正、才生成定义（fake librarian 每篇都标同样的
    两个双链）。再编译一次能把被清掉的关联补回来——存量论文重新编译即可，不用迁移。"""
    token = await register_and_login(client, email="daily-concepts@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    await _run_sync(
        monkeypatch,
        {
            "cs.AI": [
                _rss_entry("2607.00061", "Concept Me"),
                _rss_entry("2607.00062", "Concept Me Too"),
            ]
        },
    )
    items = (await client.get("/api/daily/papers", headers=headers)).json()["items"]
    item = items[0]

    for entry in items:
        resp = await client.post(
            f"/api/daily/papers/{entry['entry_id']}/compile", headers=headers
        )
        assert resp.status_code == 200, resp.text
        assert "[[" in resp.json()["wiki_content"]  # 正文里有双链

    from sqlalchemy import delete, select

    from app.core.db import get_sessionmaker
    from app.models.library_direction import LibraryPaper
    from app.models.llm_config import LLMUsage
    from app.models.paper import Concept, paper_concepts

    paper_id = uuid.UUID(item["paper_id"])

    async def _linked_concepts() -> list[Concept]:
        async with get_sessionmaker()() as session:
            return list(
                (
                    await session.execute(
                        select(Concept)
                        .join(paper_concepts, paper_concepts.c.concept_id == Concept.id)
                        .where(paper_concepts.c.paper_id == paper_id)
                    )
                )
                .scalars()
                .all()
            )

    concepts = await _linked_concepts()
    assert {c.name for c in concepts} == {"Agent", "强化学习"}  # fake librarian 的双链
    # 两篇都提到 → 转正，且转正那一刻才生成定义
    assert all(c.status == "active" for c in concepts)
    assert all(not c.definition.endswith("（定义待补充）") for c in concepts)  # 定义真的要到了

    async with get_sessionmaker()() as session:
        # 这篇论文不属于任何库，上链照样发生
        assert (await session.execute(select(LibraryPaper))).scalars().all() == []
        rows = (await session.execute(select(LLMUsage))).scalars().all()
        assert rows and all(r.library_id is None for r in rows)  # 记账落平台级，不摊给某个库
        assert any(r.stage == "extract" for r in rows)  # 概念定义那次调用记在触发人名下
        # 编译/上链这些有发起人的调用都记在人名下；每日同步那步的向量化是定时任务
        # 发起的系统级调用，本来就没有「人」，记平台账（user_id 空）
        assert all(r.user_id is not None for r in rows if r.stage != "embedding")

        # 存量（编译过但没上链）的路径：清掉关联后重新编译能补回来
        await session.execute(delete(paper_concepts).where(paper_concepts.c.paper_id == paper_id))
        await session.commit()
    assert await _linked_concepts() == []

    resp = await client.post(f"/api/daily/papers/{item['entry_id']}/compile", headers=headers)
    assert resp.status_code == 200, resp.text
    assert {c.name for c in await _linked_concepts()} == {"Agent", "强化学习"}


async def test_daily_pool_chat_sse(client, monkeypatch):
    """池对话：scope = 池内全部论文，摘要级降级（无索引），sources → delta* → done。"""
    token = await register_and_login(client)
    headers = {"Authorization": f"Bearer {token}"}
    await _run_sync(monkeypatch, {"cs.AI": [_rss_entry("2607.00051", "Chat About Me")]})

    async with client.stream(
        "POST",
        "/api/daily/chat",
        json={"question": "今天有什么值得看的论文？", "history": []},
        headers=headers,
    ) as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        body = (await resp.aread()).decode("utf-8")

    import json

    events = []
    for block in body.strip().split("\n\n"):
        event, data = None, None
        for line in block.split("\n"):
            if line.startswith("event: "):
                event = line[len("event: ") :]
            elif line.startswith("data: "):
                data = json.loads(line[len("data: ") :])
        if event is not None:
            events.append((event, data))
    kinds = [e for e, _ in events]
    assert kinds[0] == "sources" and kinds[-1] == "done" and "error" not in kinds


async def test_categories_admin_and_refresh(client, queue_stub):
    admin = await register_and_login(client)  # 首个 = admin
    member = await register_and_login(client, email="bob2@example.com")
    ah = {"Authorization": f"Bearer {admin}"}
    mh = {"Authorization": f"Bearer {member}"}

    resp = await client.get("/api/daily/categories", headers=mh)
    assert resp.json()["categories"] == ["cs.AI", "cs.CL", "cs.CV"]

    # 普通成员改分类 → 403
    resp = await client.put("/api/daily/categories", json={"categories": ["cs.LG"]}, headers=mh)
    assert resp.status_code == 403

    # admin 改分类；非法格式 422
    resp = await client.put(
        "/api/daily/categories", json={"categories": ["cs.LG", "stat.ML"]}, headers=ah
    )
    assert resp.status_code == 200 and resp.json()["categories"] == ["cs.LG", "stat.ML"]
    resp = await client.put(
        "/api/daily/categories", json={"categories": ["Not A Cat!"]}, headers=ah
    )
    assert resp.status_code == 422

    # 手动刷新建任务 + 入队（admin only），返回 voyage_id 供前端跳任务详情
    resp = await client.post("/api/daily/refresh", headers=mh)
    assert resp.status_code == 403
    resp = await client.post("/api/daily/refresh", headers=ah)
    assert resp.status_code == 202
    voyage_id = resp.json()["voyage_id"]
    assert queue_stub.jobs == [("run_voyage", (voyage_id,), {})]

    # 全局单例：上一次还在跑时再点 → 409
    resp = await client.post("/api/daily/refresh", headers=ah)
    assert resp.status_code == 409 and resp.json()["detail"] == "DAILY_FEED_RUNNING"


# ---- 纳入任务系统（kind=daily_feed_sync）----


async def test_daily_feed_voyage_is_platform_scoped_and_singleton(client):
    """建出来的任务：kind 正确、两个作用域 id 都为空；在跑时再建 → 冲突。"""
    from app.core.db import get_sessionmaker
    from app.services import daily_feed

    async with get_sessionmaker()() as session:
        run = await daily_feed.create_daily_feed_voyage(session, created_by=None)
        assert run.kind == "daily_feed_sync"
        assert run.project_id is None and run.library_id is None
        assert run.budget == {"max_tokens": None}
        run_id = run.id

        with pytest.raises(daily_feed.DailyFeedConflictError):
            await daily_feed.create_daily_feed_voyage(session, created_by=None)

        # 终态后不再互斥
        found = await daily_feed.find_running_daily_feed_voyage(session)
        assert found is not None and found.id == run_id
        found.status = "done"
        await session.commit()
        assert await daily_feed.find_running_daily_feed_voyage(session) is None
        again = await daily_feed.create_daily_feed_voyage(session, created_by=None)
        assert again.id != run_id


async def _async_value(value):
    """把普通值包成 awaitable：get_task_queue 是 async 函数，桩也得是。"""
    return value


async def test_daily_feed_plan_is_the_five_fixed_steps():
    from app.agents.voyage.actions import known_actions
    from app.agents.voyage.navigator import daily_feed_plan
    from app.models.voyage import VoyageRun, mode_for_kind

    assert mode_for_kind("daily_feed_sync") == "pipeline"
    plan = daily_feed_plan(VoyageRun(kind="daily_feed_sync", goal="每日新论文抓取"))
    assert [s["action"] for s in plan] == [
        "daily.fetch",
        "daily.upsert",
        "daily.cleanup",
        "daily.embed",
        "daily.sync_libraries",
    ]
    assert all(s["checks"] == [{"kind": "no_error"}] for s in plan)
    # 动作必须已注册（app.agents.voyage.__init__ 导入 actions_daily），否则引擎查不到表
    assert {s["action"] for s in plan} <= known_actions()


def _action_ctx():
    """跑单个动作用的最小上下文（无 bus，ctx.log 静默）。"""
    import app.agents.voyage  # noqa: F401  触发 actions_daily 注册
    from app.agents.voyage.actions import ActionContext
    from app.core.llm.router import LLMRouter
    from app.models.voyage import VoyageRun

    run = VoyageRun(kind="daily_feed_sync", goal="每日新论文抓取", status="executing", cursor=0)
    run.id = uuid.uuid4()
    return ActionContext(run=run, llm=LLMRouter(), checkpoint={})


async def test_daily_actions_observation_shapes(client, monkeypatch):
    """四个动作各自的 observation 形状 + 跨步骤用 checkpoint 传值。"""
    from sqlalchemy import select

    from app.agents.voyage.actions import get_action
    from app.core.db import get_sessionmaker
    from app.models.daily_feed import DailyFeedEntry
    from app.services import daily_feed

    await register_and_login(client)
    monkeypatch.setattr(
        daily_feed,
        "get_arxiv_client",
        lambda: _StubArxiv(
            {
                "cs.AI": [
                    _rss_entry("2607.00061", "Voyage A"),
                    _rss_entry("2607.00062", "Voyage B"),
                ],
                "cs.CL": [_rss_entry("2607.00061", "Voyage A", announce="cross")],
                "cs.CV": [],
            }
        ),
    )
    ctx = _action_ctx()

    obs = await get_action("daily.fetch")(ctx, {})
    assert obs["categories"] == ["cs.AI", "cs.CL", "cs.CV"]
    assert obs["fetched"] == 3
    assert {c: s["count"] for c, s in obs["per_category"].items()} == {
        "cs.AI": 2,
        "cs.CL": 1,
        "cs.CV": 0,
    }
    assert all(s["status"] == "ok" and not s["stale"] for s in obs["per_category"].values())
    assert obs["failed_categories"] == []
    assert "error" not in obs
    assert ctx.checkpoint["daily_entries"]  # 条目交给下一步

    obs = await get_action("daily.upsert")(ctx, {})
    assert obs == {"created": 2, "merged": 1, "papers": 3}
    touched = ctx.checkpoint["daily_touched_papers"]
    assert len(touched) == 3 and all(isinstance(p, str) for p in touched)  # checkpoint 要可 JSON
    assert "daily_entries" not in ctx.checkpoint

    # 过期一条 → 清理这一步报出来
    async with get_sessionmaker()() as session:
        from sqlalchemy import select

        entry = (await session.execute(select(DailyFeedEntry))).scalars().first()
        # 比保留期更早才算过期（保留期默认 14 天，且管理员可改）
        from app.services.daily_feed import DEFAULT_RETENTION_DAYS

        entry.feed_date = entry.feed_date - dt.timedelta(days=DEFAULT_RETENTION_DAYS + 1)
        await session.commit()

    obs = await get_action("daily.cleanup")(ctx, {})
    assert obs == {"expired": 1}

    # 无条件建：论文级向量 + 无全文时的摘要兜底块（不再有管理员开关）
    obs = await get_action("daily.embed")(ctx, {})
    assert obs == {"enabled": True, "embedded": 1, "failed": 0, "chunked": 1}
    assert "error" not in obs


async def test_daily_feed_voyage_end_to_end(client, monkeypatch):
    """整条任务跑通：五步依次 passed、run 到 done、论文真的入了池、库同步被触发。"""
    from app.agents.voyage.engine import VoyageEngine
    from app.core.db import get_sessionmaker
    from app.core.llm.router import LLMRouter
    from app.models.voyage import VoyageRun
    from app.services import daily_feed
    from tests.conftest import RecordingBus

    await register_and_login(client)
    monkeypatch.setattr(
        daily_feed,
        "get_arxiv_client",
        lambda: _StubArxiv({"cs.AI": [_rss_entry("2607.00071", "E2E Paper")]}),
    )

    # 最后一步会入队库同步；测试里没有真 Redis，打桩记录调用
    from app.core import queue as queue_module

    enqueued: list[tuple] = []

    class _StubQueue:
        async def enqueue(self, func, *args, **kwargs):
            enqueued.append((func, args))

    monkeypatch.setattr(queue_module, "get_task_queue", lambda: _async_value(_StubQueue()))

    async with get_sessionmaker()() as session:
        run = await daily_feed.create_daily_feed_voyage(session, created_by=None)
        run_id = run.id

    await VoyageEngine(event_bus=RecordingBus(), llm_router=LLMRouter()).run(run_id)

    assert enqueued == [("daily_wiki_ingest", ())], "入池后必须触发文献库同步"

    async with get_sessionmaker()() as session:
        from sqlalchemy import select
        from sqlalchemy.orm import selectinload

        run = (
            await session.execute(
                select(VoyageRun)
                .where(VoyageRun.id == run_id)
                .options(selectinload(VoyageRun.steps))
            )
        ).scalar_one()
        assert run.status == "done", run.status
        assert run.mode == "pipeline"
        assert [s.action for s in run.steps] == [
            "daily.fetch",
            "daily.upsert",
            "daily.cleanup",
            "daily.embed",
            "daily.sync_libraries",
        ]
        assert all(s.status == "passed" for s in run.steps)

    token = await register_and_login(client, email="e2e@example.com")
    resp = await client.get(
        "/api/daily/papers", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.json()["total"] == 1


async def test_weekend_batch_is_not_reported_as_stale(client, monkeypatch):
    """周六周日 arXiv 不公告，拿到周五那批是正常的，不该报「抓早了」。

    这条豁免以前只能靠「挑个周末跑测试」验证——它反过来还让另一条测试每逢周末必挂。
    """
    import datetime as dt

    from app.agents.voyage import actions_daily
    from app.agents.voyage.actions import get_action
    from app.services import daily_feed

    await register_and_login(client)
    friday = dt.datetime.now(dt.UTC).date() - dt.timedelta(days=1)
    monkeypatch.setattr(
        daily_feed,
        "get_arxiv_client",
        lambda: _StubArxiv({"cs.AI": [_rss_entry("2607.00131", "Friday")]}, batch_date=friday),
    )
    monkeypatch.setattr(actions_daily, "_is_weekend", lambda *a, **k: True)

    obs = await get_action("daily.fetch")(_action_ctx(), {})
    assert obs["stale_batch_dates"] == []
    assert "error" not in obs and "note" not in obs


async def test_is_weekend_follows_the_calendar():
    """判据本身：周六周日为真，周一到周五为假。"""
    import datetime as dt

    from app.agents.voyage.actions_daily import _is_weekend

    monday = dt.datetime(2026, 7, 27, 12, 0, tzinfo=dt.UTC)
    for offset, expected in enumerate([False, False, False, False, False, True, True]):
        assert _is_weekend(monday + dt.timedelta(days=offset)) is expected, offset


async def test_no_new_papers_does_not_trigger_library_sync(client, monkeypatch):
    """池里一篇新论文都没有（周末/节假日/当天没发）就不触发文献库同步。

    否则 14 个库各跑一轮打分与编译，扫的是同一批昨天就打过分的论文，白花一轮钱，
    还在每个库下面留一条「0 篇新论文」的任务记录。
    """
    from app.agents.voyage.actions import get_action
    from app.agents.voyage.checks import run_deterministic_checks

    await register_and_login(client)
    enqueued: list[str] = []

    class _Queue:
        async def enqueue(self, name, *args, **kwargs):
            enqueued.append(name)

    from app.core import queue as queue_mod

    async def _get_queue():
        return _Queue()

    monkeypatch.setattr(queue_mod, "get_task_queue", _get_queue)

    ctx = _action_ctx()
    ctx.checkpoint["daily_created"] = 0
    obs = await get_action("daily.sync_libraries")(ctx, {})

    assert obs["triggered"] is False
    assert obs["reason"] == "no_new_papers"
    assert enqueued == [], "没有新论文就不该入队库同步"

    # 跳过是正常结果，不能把这一步判成失败
    verdict, _ = run_deterministic_checks(
        [{"kind": "no_error"}], observation=obs, checkpoint=ctx.checkpoint
    )
    assert verdict is not None and verdict["passed"] is True


async def test_new_papers_still_trigger_library_sync(client, monkeypatch):
    """有新论文就照常触发——跳过的判据不能把正常的一轮也挡掉。"""
    from app.agents.voyage.actions import get_action

    await register_and_login(client)
    enqueued: list[str] = []

    class _Queue:
        async def enqueue(self, name, *args, **kwargs):
            enqueued.append(name)

    from app.core import queue as queue_mod

    async def _get_queue():
        return _Queue()

    monkeypatch.setattr(queue_mod, "get_task_queue", _get_queue)

    ctx = _action_ctx()
    ctx.checkpoint["daily_created"] = 7
    obs = await get_action("daily.sync_libraries")(ctx, {})

    assert obs["triggered"] is True and obs["created"] == 7
    assert enqueued == ["daily_wiki_ingest"]


async def test_upsert_records_the_new_paper_count_for_the_next_step(client, monkeypatch):
    """入池步骤要把「新增几篇」写进 checkpoint——触发与否的判据来自它，不是抓到的条目数。

    抓到几百条但全是昨天入过池的，去重后 created=0，对文献库来说和没抓一样。
    """
    from app.agents.voyage.actions import get_action

    await register_and_login(client)
    ctx = _action_ctx()
    ctx.checkpoint["daily_entries"] = {"cs.AI": [_rss_entry("2607.00120", "Brand new")]}
    obs = await get_action("daily.upsert")(ctx, {})
    assert obs["created"] == 1
    assert ctx.checkpoint["daily_created"] == 1

    # 同一篇再来一次：去重后没有新增，checkpoint 如实记 0
    ctx2 = _action_ctx()
    ctx2.checkpoint["daily_entries"] = {"cs.AI": [_rss_entry("2607.00120", "Brand new")]}
    obs2 = await get_action("daily.upsert")(ctx2, {})
    assert obs2["created"] == 0
    assert ctx2.checkpoint["daily_created"] == 0


async def test_daily_fetch_fails_when_any_category_errors(client, monkeypatch):
    """**任一分类抓失败就报错**，不是「全都空才报」。

    部分失败下当天那个分类的论文会永久缺失（RSS 只公告一次、每日池只留一周），而全
    实验室的文献库都靠这个池供料——静默残缺比整体失败更危险。
    """
    from app.agents.voyage.actions import get_action
    from app.agents.voyage.checks import run_deterministic_checks
    from app.services import daily_feed

    class _PartlyBroken:
        async def fetch_new(self, category: str):
            import datetime as _dt

            if category == "cs.AI":
                raise RuntimeError("429 Too Many Requests")
            today = _dt.datetime.now(_dt.UTC).replace(hour=0, minute=0, second=0, microsecond=0)
            return [_rss_entry("2607.00099", "Fine")], today

    await register_and_login(client)
    monkeypatch.setattr(daily_feed, "get_arxiv_client", lambda: _PartlyBroken())

    ctx = _action_ctx()
    obs = await get_action("daily.fetch")(ctx, {})
    assert obs["failed_categories"] == ["cs.AI"]
    assert obs["per_category"]["cs.AI"]["status"] == "error"
    assert "429" in obs["per_category"]["cs.AI"]["detail"]
    # 其余分类照常抓到，不受影响
    assert obs["per_category"]["cs.CL"]["status"] == "ok"
    assert obs["error"]

    verdict, _ = run_deterministic_checks(
        [{"kind": "no_error"}], observation=obs, checkpoint=ctx.checkpoint
    )
    assert verdict is not None and verdict["passed"] is False


async def test_daily_fetch_notes_but_does_not_fail_on_a_quiet_day(client, monkeypatch):
    """全部分类都抓成功但一篇没有：周末/节假日属正常，给提示而不是报错。"""
    from app.agents.voyage.actions import get_action
    from app.agents.voyage.checks import run_deterministic_checks
    from app.services import daily_feed

    await register_and_login(client)
    monkeypatch.setattr(daily_feed, "get_arxiv_client", lambda: _StubArxiv({}))

    ctx = _action_ctx()
    obs = await get_action("daily.fetch")(ctx, {})
    assert obs["fetched"] == 0
    assert obs["failed_categories"] == []
    assert "error" not in obs
    assert obs["note"]

    verdict, _ = run_deterministic_checks(
        [{"kind": "no_error"}], observation=obs, checkpoint=ctx.checkpoint
    )
    assert verdict is None or verdict["passed"] is True




async def test_sync_status_reports_failed_categories(client, monkeypatch):
    """同步状况要能把「某分类抓失败」摆到界面上，而不是只留在任务日志里。"""
    from app.core.db import get_sessionmaker
    from app.models.voyage import VoyageRun, VoyageStep
    from app.services import daily_feed

    await register_and_login(client)

    async with get_sessionmaker()() as session:
        run = VoyageRun(kind="daily_feed_sync", status="paused_error", goal="每日抓取")
        session.add(run)
        await session.flush()
        session.add(
            VoyageStep(
                run_id=run.id,
                seq=0,
                title="抓取",
                action="daily.fetch",
                status="failed",
                observation={
                    "per_category": {
                        "cs.AI": {"count": 0, "status": "error", "detail": "429"},
                        "cs.CL": {"count": 12, "status": "ok", "detail": None},
                    },
                    "failed_categories": ["cs.AI"],
                },
            )
        )
        await session.commit()

    async with get_sessionmaker()() as session:
        status = await daily_feed.sync_status(session)
    assert status["failed_categories"] == ["cs.AI"]
    assert status["last_run_status"] == "paused_error"
    assert status["per_category"]["cs.CL"]["count"] == 12
    # 有分类抓失败时优先报失败（比「停更」更接近原因，也更可操作）；
    # stale 现在只在 stalled 时为真，所以这里是 False。
    assert status["feed_state"] == "failed"
    assert status["stale"] is False

    resp = await client.get(
        "/api/daily/sync-status",
        headers={"Authorization": f"Bearer {await register_and_login(client, email='v@e.com')}"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["failed_categories"] == ["cs.AI"]


# ---- 抓取时刻 ----


async def test_default_probe_start_time(client):
    """默认从 UTC 01:30（北京 09:30）开始探，之后每 15 分钟一次直到当天批次出现。

    早开始是安全的——探到旧批次就什么都不做。定死一个抓取时刻才危险：发布时刻会飘，
    赌输的表现是拿到上一批、去重后一条不进、每一步却都报成功。
    """
    from app.core.db import get_sessionmaker
    from app.services import daily_feed

    async with get_sessionmaker()() as session:
        assert await daily_feed.get_sync_time(session) == (1, 30)


async def test_sync_time_is_admin_configurable(client):
    from app.core.db import get_sessionmaker
    from app.services import daily_feed

    admin = {"Authorization": f"Bearer {await register_and_login(client)}"}  # 首个用户=admin
    resp = await client.get("/api/daily/sync-time", headers=admin)
    assert resp.status_code == 200
    assert resp.json() == {"hour": 1, "minute": 30}

    resp = await client.put("/api/daily/sync-time", json={"hour": 3, "minute": 45}, headers=admin)
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"hour": 3, "minute": 45}

    async with get_sessionmaker()() as session:
        assert await daily_feed.get_sync_time(session) == (3, 45)

    # 非 admin 改不了
    other = {"Authorization": f"Bearer {await register_and_login(client, email='u2@e.com')}"}
    resp = await client.put("/api/daily/sync-time", json={"hour": 5, "minute": 0}, headers=other)
    assert resp.status_code == 403


async def test_due_now_gates_on_the_configured_time(client):
    """检查点每 15 分钟跑一次，靠 due_now 判断到点没有——没有它就会一天触发几十次。"""
    import datetime as dt

    from app.core.db import get_sessionmaker
    from app.services import daily_feed

    async with get_sessionmaker()() as session:
        day = dt.datetime(2026, 7, 28, tzinfo=dt.UTC)
        assert not await daily_feed.due_now(session, now=day.replace(hour=1, minute=15))
        assert await daily_feed.due_now(session, now=day.replace(hour=1, minute=30))
        assert await daily_feed.due_now(session, now=day.replace(hour=9, minute=0))


async def test_claim_today_is_idempotent_within_a_day(client):
    import datetime as dt

    from app.core.db import get_sessionmaker
    from app.services import daily_feed

    now = dt.datetime(2026, 7, 28, 5, 0, tzinfo=dt.UTC)
    async with get_sessionmaker()() as session:
        assert await daily_feed.claim_today(session, "k", now=now) is True
        assert await daily_feed.claim_today(session, "k", now=now) is False
        # 换一天又能占
        assert await daily_feed.claim_today(session, "k", now=now + dt.timedelta(days=1)) is True


async def test_day_counts_follow_the_active_filters(client, monkeypatch):
    """日期标签上的数字要跟着筛选走。

    以前 /daily/days 是无条件按天统计的，于是选了某个分类之后，标签仍显示全部篇数，
    和下面列表里看到的对不上——看起来就像筛选根本没生效。
    """
    from app.core.db import get_sessionmaker
    from app.services import daily_feed

    headers = {"Authorization": f"Bearer {await register_and_login(client)}"}
    monkeypatch.setattr(
        daily_feed,
        "get_arxiv_client",
        lambda: _StubArxiv(
            {
                "cs.AI": [
                    _rss_entry("2607.11001", "AI One"),
                    _rss_entry("2607.11002", "AI Two", announce="cross"),
                ],
                "cs.CV": [_rss_entry("2607.11003", "CV One")],
            }
        ),
    )
    async with get_sessionmaker()() as session:
        _, by_category, _ = await daily_feed.fetch_new_by_category(session)
        await daily_feed.upsert_entries(session, by_category=by_category)

    async def days(**params):
        resp = await client.get("/api/daily/days", params=params, headers=headers)
        assert resp.status_code == 200, resp.text
        return sum(d["count"] for d in resp.json())

    everything = await days()
    assert everything == 3

    # 按分类筛：只剩该分类的条目
    assert await days(category="cs.CV") == 1
    assert await days(category="cs.AI") == 2

    # 按公告类型筛
    assert await days(announce="cross") == 1
    assert await days(announce="new") == 2

    # 与列表口径一致：同样的筛选下，两个数字必须相等
    resp = await client.get(
        "/api/daily/papers", params={"category": "cs.CV", "size": 50}, headers=headers
    )
    assert resp.json()["total"] == await days(category="cs.CV")


async def test_stale_batch_is_visible_but_does_not_fail_the_run(client, monkeypatch):
    """抓到的不是今天那批公告 → 留痕，但**不算失败**。

    两个方向都要防：

    - 不能静默。生产上真实发生过：抓取跑在 arXiv 发布之前，RSS 返回上一批，条目数照样
      两三百，但全都昨天入过池，去重后 created=0——而每一步都报 passed，三天下来丢了
      两整天的论文，没有任何一处报警。所以 stale_batch_dates 与 note 必须留下。
    - 也不能报错。「今天那批还没发布」是轮询语义下的正常状态：检查点会继续探，手动
      触发也可能落在发布之前。以前这里置 error，一次早点的手动触发就留下一条
      paused_error，而它其实什么都没坏。
    """
    import datetime as dt

    from app.agents.voyage.actions import get_action
    from app.agents.voyage.checks import run_deterministic_checks
    from app.services import daily_feed

    await register_and_login(client)
    yesterday = dt.datetime.now(dt.UTC).date() - dt.timedelta(days=1)
    monkeypatch.setattr(
        daily_feed,
        "get_arxiv_client",
        lambda: _StubArxiv({"cs.AI": [_rss_entry("2607.00081", "Stale")]}, batch_date=yesterday),
    )
    # 固定成工作日：周末 arXiv 本来就不公告，那条豁免会让这条断言随星期时对时错
    from app.agents.voyage import actions_daily

    monkeypatch.setattr(actions_daily, "_is_weekend", lambda *a, **k: False)

    ctx = _action_ctx()
    obs = await get_action("daily.fetch")(ctx, {})
    assert obs["stale_batch_dates"] == [yesterday.isoformat()]
    assert obs["per_category"]["cs.AI"]["stale"] is True
    assert "error" not in obs, "还没发布不是故障"
    assert obs["note"] and "今天那批还没发布" in obs["note"]

    verdict, _ = run_deterministic_checks(
        [{"kind": "no_error"}], observation=obs, checkpoint=ctx.checkpoint
    )
    assert verdict is not None and verdict["passed"] is True


async def test_probe_stops_after_the_configured_number_of_attempts(client, monkeypatch):
    """探满上限就当天收工：不再探、不建任务，也不留失败记录。

    「今天 arXiv 就是没发」是正常情况（周末、节假日、发布故障）。没有上限时检查点会
    从早空转到晚，每 15 分钟打一次 arXiv，却永远得不出结论。
    """
    import datetime as dt

    from app.core.db import get_sessionmaker
    from app.services import daily_feed
    from worker.tasks import daily_feed_sync

    await register_and_login(client)
    yesterday = dt.datetime.now(dt.UTC).date() - dt.timedelta(days=1)
    monkeypatch.setattr(
        daily_feed, "get_arxiv_client", lambda: _StubArxiv({}, batch_date=yesterday)
    )
    async with get_sessionmaker()() as session:
        await daily_feed.set_sync_time(session, 0, 0)  # 保证 due_now 恒真
        await daily_feed.set_max_probe_attempts(session, 3)

    enqueued: list[str] = []

    class _Redis:
        async def enqueue_job(self, name, *args, **kwargs):
            enqueued.append(name)

    ctx = {"redis": _Redis()}
    state: dict = {}
    for expected in (1, 2, 3):
        assert await daily_feed_sync(ctx) is None
        async with get_sessionmaker()() as session:
            state = await daily_feed.probe_state(session, now=dt.datetime.now(dt.UTC))
        assert state["attempts"] == expected
        assert state["batch_date"] == yesterday.isoformat()
    assert state["exhausted"] is True

    # 第四次：已经收工，连 arXiv 都不该再打
    probed: list[str] = []

    class _Counting:
        async def fetch_new(self, category: str):
            probed.append(category)
            raise AssertionError("探满之后不该再探")

    monkeypatch.setattr(daily_feed, "get_arxiv_client", lambda: _Counting())
    assert await daily_feed_sync(ctx) is None
    assert probed == []
    assert enqueued == [], "全程不该建任何任务"


async def test_probe_creates_the_run_once_the_batch_appears(client, monkeypatch):
    """探到今天那批就建任务——次数上限不能把正常的抓取也挡掉。"""
    import datetime as dt

    from app.core.db import get_sessionmaker
    from app.services import daily_feed
    from worker.tasks import daily_feed_sync

    await register_and_login(client)
    yesterday = dt.datetime.now(dt.UTC).date() - dt.timedelta(days=1)
    monkeypatch.setattr(
        daily_feed, "get_arxiv_client", lambda: _StubArxiv({}, batch_date=yesterday)
    )
    async with get_sessionmaker()() as session:
        await daily_feed.set_sync_time(session, 0, 0)
        await daily_feed.set_max_probe_attempts(session, 5)

    enqueued: list[tuple[str, str]] = []

    class _Redis:
        async def enqueue_job(self, name, run_id, **kwargs):
            enqueued.append((name, run_id))

    ctx = {"redis": _Redis()}
    assert await daily_feed_sync(ctx) is None  # 还没发布

    # 批次「出现」= 有我们还没收的论文，而**不是**声明日期变成了今天：arXiv 的日期
    # 字段会滞后一天，按日期判会永远等下去。
    monkeypatch.setattr(
        daily_feed,
        "get_arxiv_client",
        lambda: _StubArxiv({"cs.AI": [_rss_entry("2608.66001", "新到的")]}, batch_date=yesterday),
    )
    run_id = await daily_feed_sync(ctx)
    assert run_id is not None
    assert enqueued == [("run_voyage", run_id)]


async def test_max_probe_attempts_defaults_to_ten_and_is_configurable(client):
    """默认 10 次；管理员可改，普通用户只读。"""
    admin = {"Authorization": f"Bearer {await register_and_login(client)}"}
    member = {
        "Authorization": f"Bearer {await register_and_login(client, email='probe@example.com')}"
    }

    resp = await client.get("/api/daily/probe-attempts", headers=member)
    assert resp.status_code == 200 and resp.json()["attempts"] == 10

    assert (
        await client.put("/api/daily/probe-attempts", json={"attempts": 4}, headers=member)
    ).status_code == 403

    resp = await client.put("/api/daily/probe-attempts", json={"attempts": 4}, headers=admin)
    assert resp.status_code == 200 and resp.json()["attempts"] == 4
    assert (await client.get("/api/daily/probe-attempts", headers=member)).json()["attempts"] == 4

    for bad in (0, 97):
        resp = await client.put("/api/daily/probe-attempts", json={"attempts": bad}, headers=admin)
        assert resp.status_code == 422, bad


async def test_weekend_pool_is_quiet_not_stalled(client):
    """周日看到「最新是周五」不该报停更——arXiv 周末本来就不公告。

    界面上一度只有一个「论文池已停更」，把「周末没发」「今天还没发」「真的落后了」
    混成一句话，于是每个周末都在报一次假警。
    """
    import datetime as dt

    from app.services.daily_feed import feed_state

    await register_and_login(client)
    friday, saturday, sunday = dt.date(2026, 7, 31), dt.date(2026, 8, 1), dt.date(2026, 8, 2)
    idle = {"attempts": 0, "batch_date": None, "exhausted": False}
    # 周末拿着周五那批就是**已跟上**：更新的批次根本不存在，没什么可等的
    assert feed_state(latest=friday, today=saturday, probe=idle, max_attempts=10) == "fresh"
    assert feed_state(latest=friday, today=sunday, probe=idle, max_attempts=10) == "fresh"
    assert feed_state(latest=friday, today=friday, probe=idle, max_attempts=10) == "fresh"
    # 周末而池子还停在更早的日子（周四）→ 也不催：周末补不上，等周一
    thursday = dt.date(2026, 7, 30)
    assert feed_state(latest=thursday, today=sunday, probe=idle, max_attempts=10) == "quiet"


async def test_waiting_for_todays_batch_is_not_stalled(client):
    """工作日、检查点还在探的时候是「等待」，不是故障。"""
    import datetime as dt

    from app.services.daily_feed import feed_state

    await register_and_login(client)
    monday, tuesday = dt.date(2026, 8, 3), dt.date(2026, 8, 4)
    probing = {"attempts": 3, "batch_date": monday.isoformat(), "exhausted": False}
    assert feed_state(latest=monday, today=tuesday, probe=probing, max_attempts=10) == "waiting"

    # 探满了，而探到的最新批次就是池子里这批 → arXiv 自己没发，我们没落后
    done = {"attempts": 10, "batch_date": monday.isoformat(), "exhausted": True}
    assert feed_state(latest=monday, today=tuesday, probe=done, max_attempts=10) == "quiet"


async def test_genuinely_behind_is_reported_as_stalled(client):
    """该公告的日子过去了、池子没跟上、又没有「arXiv 没发」的证据 → 这才要提醒人。"""
    import datetime as dt

    from app.services.daily_feed import feed_state

    await register_and_login(client)
    exhausted_no_evidence = {"attempts": 10, "batch_date": None, "exhausted": True}
    assert (
        feed_state(
            latest=dt.date(2026, 7, 28),
            today=dt.date(2026, 8, 4),
            probe=exhausted_no_evidence,
            max_attempts=10,
        )
        == "stalled"
    )
    # 池子空 → 一样要提醒
    assert (
        feed_state(
            latest=None, today=dt.date(2026, 8, 4), probe=exhausted_no_evidence, max_attempts=10
        )
        == "stalled"
    )
    # 有分类抓失败时优先报失败
    assert (
        feed_state(
            latest=dt.date(2026, 8, 4),
            today=dt.date(2026, 8, 4),
            probe=exhausted_no_evidence,
            max_attempts=10,
            failed=True,
        )
        == "failed"
    )


async def test_last_publishing_day_skips_the_weekend(client):
    import datetime as dt

    from app.services.daily_feed import last_publishing_day

    await register_and_login(client)
    friday = dt.date(2026, 7, 31)
    assert last_publishing_day(dt.date(2026, 8, 1)) == friday  # 周六
    assert last_publishing_day(dt.date(2026, 8, 2)) == friday  # 周日
    assert last_publishing_day(friday) == friday
    assert last_publishing_day(dt.date(2026, 8, 3)) == dt.date(2026, 8, 3)  # 周一


async def test_sync_status_reports_the_feed_state(client, monkeypatch):
    """接口要把状态发出来，而且 stale 只在 stalled 时为真（前端旧口径靠它）。"""
    headers = {"Authorization": f"Bearer {await register_and_login(client)}"}
    body = (await client.get("/api/daily/sync-status", headers=headers)).json()
    assert body["feed_state"] in {"fresh", "waiting", "quiet", "stalled", "failed"}
    assert body["stale"] is (body["feed_state"] == "stalled")


async def test_sync_status_shows_how_far_the_probe_has_got(client, monkeypatch):
    """界面要能区分「还在探」和「探完了今天就是没有」，否则用户不知道该等还是该查。"""
    import datetime as dt

    from app.core.db import get_sessionmaker
    from app.services import daily_feed

    headers = {"Authorization": f"Bearer {await register_and_login(client)}"}
    yesterday = dt.datetime.now(dt.UTC).date() - dt.timedelta(days=1)

    body = (await client.get("/api/daily/sync-status", headers=headers)).json()
    assert body["probe_attempts"] == 0 and body["probe_max_attempts"] == 10

    async with get_sessionmaker()() as session:
        await daily_feed.record_probe(
            session, now=dt.datetime.now(dt.UTC), batch_date=yesterday.isoformat()
        )

    body = (await client.get("/api/daily/sync-status", headers=headers)).json()
    assert body["probe_attempts"] == 1
    assert body["probe_batch_date"] == yesterday.isoformat()
    assert body["probe_exhausted"] is False


async def test_fresh_batch_is_not_reported_as_stale(client, monkeypatch):
    from app.agents.voyage.actions import get_action
    from app.services import daily_feed

    await register_and_login(client)
    monkeypatch.setattr(
        daily_feed,
        "get_arxiv_client",
        lambda: _StubArxiv({"cs.AI": [_rss_entry("2607.00082", "Fresh")]}),
    )
    obs = await get_action("daily.fetch")(_action_ctx(), {})
    assert obs["stale_batch_dates"] == []
    assert "error" not in obs


async def test_probe_reports_whether_todays_batch_is_out(client, monkeypatch):
    """轮询的判据：探到的批次日期是不是今天。"""
    import datetime as dt

    from app.core.db import get_sessionmaker
    from app.services import daily_feed

    await register_and_login(client)
    yesterday = dt.datetime.now(dt.UTC).date() - dt.timedelta(days=1)

    # 空批次 = 没有可收的（周末/节假日的正常形态），日期照常如实报出来
    monkeypatch.setattr(
        daily_feed, "get_arxiv_client", lambda: _StubArxiv({}, batch_date=yesterday)
    )
    async with get_sessionmaker()() as session:
        fresh, seen = await daily_feed.todays_batch_available(session)
    assert fresh is False and seen == yesterday.isoformat()

    # 有我们没收过的论文 = 该抓了。**与它声明哪天无关**：arXiv 的日期字段会滞后一天，
    # 按日期判会永远判成「还没发」——生产上池子就是这么停在昨天的。
    monkeypatch.setattr(
        daily_feed,
        "get_arxiv_client",
        lambda: _StubArxiv(
            {"cs.AI": [_rss_entry("2608.77001", "没收过的")]}, batch_date=yesterday
        ),
    )
    async with get_sessionmaker()() as session:
        fresh, _ = await daily_feed.todays_batch_available(session)
    assert fresh is True


async def test_probe_failure_does_not_block_fetching(client, monkeypatch):
    """探测本身失败时按「可以抓」处理——不能因为一个判断失灵就再也不抓了。"""
    from app.core.db import get_sessionmaker
    from app.services import daily_feed

    class _Broken:
        async def fetch_new(self, category: str):
            raise RuntimeError("network down")

    await register_and_login(client)
    monkeypatch.setattr(daily_feed, "get_arxiv_client", lambda: _Broken())
    async with get_sessionmaker()() as session:
        fresh, seen = await daily_feed.todays_batch_available(session)
    assert fresh is True and seen is None


async def test_retention_defaults_to_two_weeks_and_is_configurable(client):
    """保留期默认 14 天，管理员可改。

    它不只是「每日页显示几天」：每日池同时是**库同步的取数窗口**（同步全量重扫），
    所以保留期就是一个文献库最多能漏几天还能自愈。
    """
    from app.core.db import get_sessionmaker
    from app.services import daily_feed

    admin = {"Authorization": f"Bearer {await register_and_login(client)}"}
    resp = await client.get("/api/daily/retention", headers=admin)
    assert resp.status_code == 200 and resp.json() == {"days": 14}

    resp = await client.put("/api/daily/retention", json={"days": 21}, headers=admin)
    assert resp.status_code == 200 and resp.json() == {"days": 21}
    async with get_sessionmaker()() as session:
        assert await daily_feed.get_retention_days(session) == 21

    other = {"Authorization": f"Bearer {await register_and_login(client, email='r2@e.com')}"}
    assert (
        await client.put("/api/daily/retention", json={"days": 3}, headers=other)
    ).status_code == 403


async def test_cleanup_uses_the_configured_retention(client, monkeypatch):
    """改了保留期，清理的截止线跟着走——两者读同一个设置。"""
    import datetime as dt

    from app.core.db import get_sessionmaker
    from app.models.daily_feed import DailyFeedEntry
    from app.models.paper import new_paper
    from app.services import daily_feed

    await register_and_login(client)
    today = dt.datetime.now(dt.UTC).date()
    async with get_sessionmaker()() as session:
        for i, age in enumerate((3, 10, 20)):
            paper = new_paper(source="arxiv", arxiv_id=f"2607.66{i:03d}", title=f"P{age}")
            session.add(paper)
            await session.flush()
            session.add(
                DailyFeedEntry(
                    paper_id=paper.id,
                    feed_date=today - dt.timedelta(days=age),
                    primary_category="cs.AI",
                )
            )
        await session.commit()

    # 默认 14 天：20 天前那条过期，10 天前的留下
    async with get_sessionmaker()() as session:
        expired = await daily_feed.cleanup_expired(session)
        await session.commit()
    assert expired == 1

    # 收紧到 7 天：10 天前那条也该被清掉
    async with get_sessionmaker()() as session:
        await daily_feed.set_retention_days(session, 7)
    async with get_sessionmaker()() as session:
        expired = await daily_feed.cleanup_expired(session)
        await session.commit()
    assert expired == 1


async def test_collected_filter_shows_only_library_members(client, monkeypatch):
    """「已收录」= 被任意文献库真正收进去的；候选与回收站不算。这是每日页的默认视角。"""
    from sqlalchemy import select

    from app.core.db import get_sessionmaker
    from app.models.library_direction import DirectionLibrary, LibraryPaper
    from app.models.paper import Paper
    from app.services import daily_feed

    headers = {"Authorization": f"Bearer {await register_and_login(client)}"}
    monkeypatch.setattr(
        daily_feed,
        "get_arxiv_client",
        lambda: _StubArxiv(
            {
                "cs.CL": [
                    _rss_entry("2608.21001", "Collected Paper"),
                    _rss_entry("2608.21002", "Candidate Paper"),
                    _rss_entry("2608.21003", "Loose Paper"),
                ]
            }
        ),
    )
    async with get_sessionmaker()() as session:
        _, by_category, _ = await daily_feed.fetch_new_by_category(session)
        await daily_feed.upsert_entries(session, by_category=by_category)
        lib = DirectionLibrary(name="收录测试库", definition={"statement": "x"})
        session.add(lib)
        await session.flush()
        papers = {
            p.title: p
            for p in (
                (await session.execute(select(Paper).where(Paper.title.like("%Paper%"))))
                .scalars()
                .all()
            )
        }
        session.add(
            LibraryPaper(
                library_id=lib.id, paper_id=papers["Collected Paper"].id, status="included"
            )
        )
        # candidate 不算收录
        session.add(
            LibraryPaper(
                library_id=lib.id, paper_id=papers["Candidate Paper"].id, status="candidate"
            )
        )
        await session.commit()

    resp = await client.get(
        "/api/daily/papers", params={"collected": "true", "size": 50}, headers=headers
    )
    titles = [item["title"] for item in resp.json()["items"]]
    assert titles == ["Collected Paper"], f"只该剩真正收录的：{titles}"

    # 日期标签的数字同口径
    days = (
        await client.get("/api/daily/days", params={"collected": "true"}, headers=headers)
    ).json()
    assert sum(d["count"] for d in days) == 1


async def test_daily_list_puts_cscl_first_and_csro_last(client, monkeypatch):
    """列表按分类优先级排：cs.CL 的在前，cs.RO 的在最后，其余居中。

    一篇同时挂 cs.CL 和 cs.RO 的按 cs.CL 算（排前面）。
    """
    from app.core.db import get_sessionmaker
    from app.services import daily_feed

    headers = {"Authorization": f"Bearer {await register_and_login(client)}"}
    monkeypatch.setattr(
        daily_feed,
        "get_arxiv_client",
        lambda: _StubArxiv(
            {
                "cs.RO": [_rss_entry("2608.22001", "Robot Paper")],
                "cs.AI": [_rss_entry("2608.22002", "AI Paper")],
                "cs.CL": [
                    _rss_entry("2608.22003", "Language Paper"),
                    # 同一篇也出现在 cs.RO：合并后同时挂两个分类，按 cs.CL 排前面
                    _rss_entry("2608.22004", "Both Paper"),
                ],
            }
        ),
    )
    async with get_sessionmaker()() as session:
        # cs.RO 不在默认订阅里，先配上，否则抓取不会去拉它
        await daily_feed.set_categories(session, ["cs.CL", "cs.AI", "cs.RO"])
        _, by_category, _ = await daily_feed.fetch_new_by_category(session)
        await daily_feed.upsert_entries(session, by_category=by_category)
        # 把 Both Paper 也并进 cs.RO 的分类（模拟跨列合并）
        by_ro = {"cs.RO": [_rss_entry("2608.22004", "Both Paper")]}
        await daily_feed.upsert_entries(session, by_category=by_ro)

    resp = await client.get(
        "/api/daily/papers", params={"sort": "date", "size": 50}, headers=headers
    )
    titles = [item["title"] for item in resp.json()["items"]]
    cl = [titles.index(t) for t in ("Language Paper", "Both Paper")]
    other = titles.index("AI Paper")
    ro = titles.index("Robot Paper")
    assert max(cl) < other < ro, f"顺序应为 cs.CL < 其他 < cs.RO：{titles}"


async def test_new_submissions_come_before_cross_lists(client, monkeypatch):
    """同一分类里，新工作排在交叉提交（更新）前面——当天真正的新东西优先。"""
    from app.core.db import get_sessionmaker
    from app.services import daily_feed

    headers = {"Authorization": f"Bearer {await register_and_login(client)}"}
    monkeypatch.setattr(
        daily_feed,
        "get_arxiv_client",
        lambda: _StubArxiv(
            {
                "cs.CL": [
                    _rss_entry("2608.23001", "Cross One", announce="cross"),
                    _rss_entry("2608.23002", "New One"),
                    _rss_entry("2608.23003", "Cross Two", announce="cross"),
                    _rss_entry("2608.23004", "New Two"),
                ]
            }
        ),
    )
    async with get_sessionmaker()() as session:
        _, by_category, _ = await daily_feed.fetch_new_by_category(session)
        await daily_feed.upsert_entries(session, by_category=by_category)

    resp = await client.get(
        "/api/daily/papers", params={"sort": "date", "size": 50}, headers=headers
    )
    titles = [item["title"] for item in resp.json()["items"]]
    news = [titles.index(t) for t in ("New One", "New Two")]
    crosses = [titles.index(t) for t in ("Cross One", "Cross Two")]
    assert max(news) < min(crosses), f"新工作应全部排在更新前面：{titles}"


async def test_probing_slows_down_after_exhaustion_instead_of_stopping():
    """探满 ≠ 当天收工。

    arXiv 的 /new 只带**当天那一批**：今天的公告错过了，明天抓的是明天那批，这一天
    就永久没有了。所以探满只该意味着别再每 15 分钟敲，而不是从此不看——发晚了两小时
    的批次不该整天丢掉。
    """
    from app.services.daily_feed import SLOW_PROBE_MINUTES, should_probe_now

    now = dt.datetime.now(dt.UTC)
    # 没探满：照常探
    fresh = {"attempts": 3, "last_probe_at": now.isoformat()}
    assert should_probe_now(fresh, now=now, max_attempts=10)
    # 探满且刚探过：不探（这就是「别再每 15 分钟敲」）
    just_now = (now - dt.timedelta(minutes=SLOW_PROBE_MINUTES - 5)).isoformat()
    assert not should_probe_now(
        {"attempts": 10, "last_probe_at": just_now}, now=now, max_attempts=10
    )
    # 探满但过了复查间隔：再探一次
    long_ago = (now - dt.timedelta(minutes=SLOW_PROBE_MINUTES + 1)).isoformat()
    assert should_probe_now(
        {"attempts": 10, "last_probe_at": long_ago}, now=now, max_attempts=10
    )


async def test_probe_gate_is_permissive_when_the_timestamp_is_missing_or_broken():
    """没有/读不懂上次探测时刻时选择「探一次」。

    多探一次的代价是一个 RSS 请求；不探的代价可能是丢掉一整天的论文。存量状态里
    没有 last_probe_at 这个字段（这次才加的），升级当天就会走到这条路上。
    """
    from app.services.daily_feed import should_probe_now

    now = dt.datetime.now(dt.UTC)
    assert should_probe_now({"attempts": 10}, now=now, max_attempts=10)
    assert should_probe_now(
        {"attempts": 10, "last_probe_at": "not-a-time"}, now=now, max_attempts=10
    )


async def test_record_probe_stamps_the_probe_time(client):
    """降频判据要有依据：每次探测都记下时刻。"""
    from app.core.db import get_sessionmaker
    from app.services import daily_feed

    assert await register_and_login(client)
    now = dt.datetime.now(dt.UTC)
    async with get_sessionmaker()() as session:
        state = await daily_feed.record_probe(session, now=now, batch_date=None)
    assert state["last_probe_at"]
    assert dt.datetime.fromisoformat(state["last_probe_at"]).date() == now.date()


async def test_a_late_batch_is_still_picked_up_after_the_probes_ran_out(client, monkeypatch):
    """探满之后 arXiv 才发布：下一次降频复查要能把它接住，建出今天的任务。

    这是这次事故真正会丢数据的地方——/new 只带当天那一批，今天错过就永久没有了。
    """
    from app.core.db import get_sessionmaker
    from app.services import daily_feed
    from worker import tasks as worker_tasks

    assert await register_and_login(client)
    now = dt.datetime.now(dt.UTC)
    async with get_sessionmaker()() as session:
        # 今天已探满，且上次探测是两小时前（早过了复查间隔）
        await daily_feed.set_max_probe_attempts(session, 2)
        for _ in range(2):
            await daily_feed.record_probe(
                session,
                now=now - dt.timedelta(hours=2),
                batch_date=(now.date() - dt.timedelta(days=1)).isoformat(),
                exhausted=True,
            )
        # 到点了、今天还没跑过任务
        hour, minute = now.hour, now.minute
        await daily_feed.set_sync_time(session, hour, minute)

    # 这一刻 arXiv 那批终于出来了
    monkeypatch.setattr(
        daily_feed, "todays_batch_available", _fake_batch_available(now.date().isoformat())
    )
    enqueued: list[str] = []

    class _Redis:
        async def enqueue_job(self, name: str, *args: object) -> None:
            enqueued.append(f"{name}:{args[0]}")

    run_id = await worker_tasks.daily_feed_sync({"redis": _Redis()})
    assert run_id, "探满之后来的批次被丢掉了"
    assert enqueued and enqueued[0].startswith("run_voyage:")


def _fake_batch_available(batch_date: str):
    async def _available(session):  # noqa: ANN001
        return True, batch_date

    return _available


async def test_probe_looks_at_content_not_at_the_declared_date(client, monkeypatch):
    """探测判「有没有我们还没收的论文」，而不是「声明日期是不是今天」。

    生产上就是这么卡住的：arXiv 的 RSS 日期字段整体滞后一天——网页版已经是 8-06 的
    清单、条目 id 也是当天的（2608.026xx），而 channel/item 的 pubDate 都还写着 8-05。
    按日期判永远为假，池子停在昨天，界面上一直「等待今天的批次（7/10）」。
    """
    from app.core.db import get_sessionmaker
    from app.services import daily_feed

    assert await register_and_login(client)
    yesterday = dt.datetime.now(dt.UTC) - dt.timedelta(days=1)

    # 日期声明成昨天（模拟 arXiv 的滞后），内容是我们没见过的新论文
    monkeypatch.setattr(
        daily_feed,
        "get_arxiv_client",
        lambda: _StubArxiv(
            {"cs.AI": [_rss_entry("2608.90001", "全新的一篇")]}, batch_date=yesterday.date()
        ),
    )
    async with get_sessionmaker()() as session:
        fresh, declared = await daily_feed.todays_batch_available(session)
    assert fresh is True, "内容是新的就该抓，别管它声明的日期"
    assert declared == yesterday.date().isoformat()  # 日期照常如实报出来


async def test_probe_says_no_when_everything_is_already_in_the_pool(client, monkeypatch):
    """同一批重复出现时不再触发抓取——这才是「还没发新的」的真正含义。"""
    from app.core.db import get_sessionmaker
    from app.services import daily_feed

    assert await register_and_login(client)
    monkeypatch.setattr(
        daily_feed,
        "get_arxiv_client",
        lambda: _StubArxiv({"cs.AI": [_rss_entry("2608.90002", "已经收过的")]}),
    )
    async with get_sessionmaker()() as session:
        _, by_category, _ = await daily_feed.fetch_new_by_category(session)
        await daily_feed.upsert_entries(session, by_category=by_category)

        fresh, _ = await daily_feed.todays_batch_available(session)
    assert fresh is False



async def test_probe_asks_every_category_not_just_the_first(client, monkeypatch):
    """首个分类全都收过了，也要接着问后面的分类。

    2026-08-12 生产漏了一整天就是这么来的：探测只问 cs.AI，那天 cs.AI 公告的 486 篇
    我们碰巧全有，于是判定「没有可收的」，整轮同步一次都没启动——而同一时刻 cs.CL /
    cs.CV / cs.RO / cs.LG 合计 386 篇是新的。界面上只是「今日文献没更新」，日志里
    一条错误也没有，所以没人会去查。

    正式抓取本来就按全部分类取（fetch_new_by_category），探测却拿一个分类替所有分类
    作答；判据和它守护的动作口径不一致，迟早对不上。
    """
    from app.core.db import get_sessionmaker
    from app.services import daily_feed

    await register_and_login(client)

    # 先让 cs.AI 的这篇真的进池，这样它对下一次探测就是「已收过」
    seen_id = "2608.88001"
    monkeypatch.setattr(
        daily_feed,
        "get_arxiv_client",
        lambda: _StubArxiv({"cs.AI": [_rss_entry(seen_id, "已经收过的")]}),
    )
    async with get_sessionmaker()() as session:
        await daily_feed.sync_daily_feed(session)

    # cs.AI 全是收过的，cs.CV 有新的：必须判成「该抓」
    monkeypatch.setattr(
        daily_feed,
        "get_arxiv_client",
        lambda: _StubArxiv(
            {
                "cs.AI": [_rss_entry(seen_id, "已经收过的")],
                "cs.CV": [_rss_entry("2608.88002", "还没收过的")],
            }
        ),
    )
    async with get_sessionmaker()() as session:
        fresh, _ = await daily_feed.todays_batch_available(session)
    assert fresh is True, "首个分类没有新论文，就不再看别的分类了"

    # 所有分类都收过了才算没有可做的
    monkeypatch.setattr(
        daily_feed,
        "get_arxiv_client",
        lambda: _StubArxiv({"cs.AI": [_rss_entry(seen_id, "已经收过的")]}),
    )
    async with get_sessionmaker()() as session:
        fresh, _ = await daily_feed.todays_batch_available(session)
    assert fresh is False


async def test_probe_stops_at_the_first_category_with_news(client, monkeypatch):
    """命中即返回：常态下不该为了探测把每个分类都打一遍。"""
    from app.core.db import get_sessionmaker
    from app.services import daily_feed

    await register_and_login(client)
    asked: list[str] = []

    class _Counting(_StubArxiv):
        async def fetch_new(self, category: str):
            asked.append(category)
            return await super().fetch_new(category)

    monkeypatch.setattr(
        daily_feed,
        "get_arxiv_client",
        lambda: _Counting(
            {
                "cs.AI": [_rss_entry("2608.88010", "新的")],
                "cs.CV": [_rss_entry("2608.88011", "也是新的")],
            }
        ),
    )
    async with get_sessionmaker()() as session:
        fresh, _ = await daily_feed.todays_batch_available(session)
    assert fresh is True
    assert asked == ["cs.AI"], f"第一个分类就有新论文，不该继续问下去：{asked}"


async def test_feed_date_records_the_announcement_day_not_the_run_day(client, monkeypatch):
    """entry 的 feed_date 记 arXiv 哪天公告的，不记我们哪天跑的抓取。

    平时两者一致，所以看不出区别；而恰恰在数据源滞后的那天——唯一会出事的那天——
    它们不一致。按跑批日期记，昨天的论文就会被标成今天的：2026-08-13 生产页面上写着
    「8月13日 · 96 篇」，那 96 篇其实是 8-12 那批，而当天真正的 446 篇一篇没进。
    数字看着完全合理，所以没人会去查。
    """
    import datetime as dt

    from sqlalchemy import select

    from app.core.db import get_sessionmaker
    from app.models.daily_feed import DailyFeedEntry
    from app.services import daily_feed

    await register_and_login(client)
    yesterday = dt.datetime.now(dt.UTC).date() - dt.timedelta(days=1)

    monkeypatch.setattr(
        daily_feed,
        "get_arxiv_client",
        lambda: _StubArxiv(
            {"cs.AI": [_rss_entry("2608.99001", "滞后那批里的论文")]}, batch_date=yesterday
        ),
    )
    async with get_sessionmaker()() as session:
        await daily_feed.sync_daily_feed(session)

    async with get_sessionmaker()() as session:
        rows = (await session.execute(select(DailyFeedEntry))).scalars().all()
    assert len(rows) == 1
    assert rows[0].feed_date == yesterday, (
        "数据源给的是昨天那批，却被记成今天——漏收就是这样被藏起来的"
    )


async def test_stale_and_fresh_categories_keep_their_own_dates(client, monkeypatch):
    """按分类各记各的：数据源会**按分类分别**滞后，不能取一个全局日期。

    2026-08-12 生产上 cs.AI 供的是前一批、cs.CL 是当天的。取最大值会把滞后那批也
    标成当天，等于把这个故障重新藏起来。
    """
    import datetime as dt

    from sqlalchemy import select

    from app.core.db import get_sessionmaker
    from app.models.daily_feed import DailyFeedEntry
    from app.models.paper import Paper
    from app.services import daily_feed

    await register_and_login(client)
    today = dt.datetime.now(dt.UTC).date()
    yesterday = today - dt.timedelta(days=1)

    class _MixedArxiv:
        async def fetch_new(self, category: str):
            at = dt.datetime.combine(
                yesterday if category == "cs.AI" else today, dt.time(), tzinfo=dt.UTC
            )
            entry = _rss_entry(
                "2608.99010" if category == "cs.AI" else "2608.99011", f"{category} 的论文"
            )
            return ([entry] if category in ("cs.AI", "cs.CL") else []), at

    monkeypatch.setattr(daily_feed, "get_arxiv_client", lambda: _MixedArxiv())
    async with get_sessionmaker()() as session:
        await daily_feed.sync_daily_feed(session)

    async with get_sessionmaker()() as session:
        rows = (
            await session.execute(
                select(Paper.arxiv_id, DailyFeedEntry.feed_date).join(
                    DailyFeedEntry, DailyFeedEntry.paper_id == Paper.id
                )
            )
        ).all()
    by_id = dict(rows)
    assert by_id["2608.99010"] == yesterday, "滞后的分类要如实记成昨天"
    assert by_id["2608.99011"] == today


async def test_leftovers_from_yesterday_do_not_count_as_todays_batch(client, monkeypatch):
    """上一批公告**我们已经收过**时，里面剩下的零星几篇不算「今天那批出来了」。

    这是最坏的一种失败：抓取跑一轮、只捡到几十篇昨天的尾巴，而 already_ran_today
    从此把当天锁死；两小时后 arXiv 真正放出当天批次时，再也没有人去取。
    2026-08-13 生产就是这样——抓取时刻设的是 02:00 UTC（北京 10:00），而 arXiv 约
    04:00 UTC（北京 12:00）才发布，那一轮收了 96 篇尾巴，当天 446 篇一整天没进来。
    """
    import datetime as dt

    from app.core.db import get_sessionmaker
    from app.services import daily_feed

    await register_and_login(client)
    yesterday = dt.datetime.now(dt.UTC).date() - dt.timedelta(days=1)

    # 先把昨天那批收进来（于是池子里有 feed_date=昨天 的条目）
    monkeypatch.setattr(
        daily_feed,
        "get_arxiv_client",
        lambda: _StubArxiv(
            {"cs.AI": [_rss_entry("2608.95000", "昨天那批")]}, batch_date=yesterday
        ),
    )
    async with get_sessionmaker()() as session:
        await daily_feed.sync_daily_feed(session)

    # 同一批公告里又冒出一篇没见过的：这是尾巴，不是今天那批
    monkeypatch.setattr(
        daily_feed,
        "get_arxiv_client",
        lambda: _StubArxiv(
            {
                "cs.AI": [
                    _rss_entry("2608.95000", "昨天那批"),
                    _rss_entry("2608.95001", "昨天那批的尾巴"),
                ]
            },
            batch_date=yesterday,
        ),
    )
    async with get_sessionmaker()() as session:
        fresh, declared = await daily_feed.todays_batch_available(session)
    assert fresh is False, "拿到的是已收过的上一批，不该判成今天那批已发布"
    assert declared == yesterday.isoformat()


async def test_a_batch_we_missed_entirely_is_still_worth_fetching(client, monkeypatch):
    """整批没收过的旧公告仍要抓——那是最后的补救窗口。

    ``/new`` 只显示最近一次公告：今天那批一发布，昨天那批就永远拿不到了。所以
    「跳过旧批次」只能针对已经收过的，不能一刀切，否则服务挂一天就等于永久丢一天。
    """
    import datetime as dt

    from app.core.db import get_sessionmaker
    from app.services import daily_feed

    await register_and_login(client)
    yesterday = dt.datetime.now(dt.UTC).date() - dt.timedelta(days=1)

    # 池子是空的：昨天那批一篇都没收过
    monkeypatch.setattr(
        daily_feed,
        "get_arxiv_client",
        lambda: _StubArxiv(
            {"cs.AI": [_rss_entry("2608.95009", "昨天那批，我们整批没收")]},
            batch_date=yesterday,
        ),
    )
    async with get_sessionmaker()() as session:
        fresh, _ = await daily_feed.todays_batch_available(session)
    assert fresh is True, "整批没收过就该抓，这是过了今天就没有的补救机会"


async def test_todays_batch_still_triggers_once_published(client, monkeypatch):
    """当天批次一发布就照常判为「该抓」——加日期判据不能把正常路径也堵死。"""
    import datetime as dt

    from app.core.db import get_sessionmaker
    from app.services import daily_feed

    await register_and_login(client)
    today = dt.datetime.now(dt.UTC).date()

    monkeypatch.setattr(
        daily_feed,
        "get_arxiv_client",
        lambda: _StubArxiv(
            {"cs.AI": [_rss_entry("2608.95002", "今天那批")]}, batch_date=today
        ),
    )
    async with get_sessionmaker()() as session:
        fresh, declared = await daily_feed.todays_batch_available(session)
    assert fresh is True
    assert declared == today.isoformat()


async def test_probe_without_a_readable_date_falls_back_to_content(client, monkeypatch):
    """读不出公告日期时仍按内容判——不能因为日期解析不出来就整天不抓。"""
    from app.core.db import get_sessionmaker
    from app.services import daily_feed

    await register_and_login(client)

    class _NoDate(_StubArxiv):
        async def fetch_new(self, category: str):
            entries, _ = await super().fetch_new(category)
            return entries, None

    monkeypatch.setattr(
        daily_feed,
        "get_arxiv_client",
        lambda: _NoDate({"cs.AI": [_rss_entry("2608.95003", "日期不明")]}),
    )
    async with get_sessionmaker()() as session:
        fresh, declared = await daily_feed.todays_batch_available(session)
    assert fresh is True and declared is None
