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
    """按分类返回固定 RSS 条目的假客户端。"""

    def __init__(self, by_category: dict[str, list[dict]]):
        self.by_category = by_category

    async def fetch_new(self, category: str) -> list[dict]:
        return self.by_category.get(category, [])


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
        entry.feed_date = entry.feed_date - dt.timedelta(days=8)
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

    再编译一次能把被清掉的关联补回来——存量论文重新编译即可，不用迁移。"""
    token = await register_and_login(client, email="daily-concepts@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    await _run_sync(monkeypatch, {"cs.AI": [_rss_entry("2607.00061", "Concept Me")]})
    item = (await client.get("/api/daily/papers", headers=headers)).json()["items"][0]

    resp = await client.post(f"/api/daily/papers/{item['entry_id']}/compile", headers=headers)
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
    assert all(not c.definition.endswith("（定义待补充）") for c in concepts)  # 定义真的要到了

    async with get_sessionmaker()() as session:
        # 这篇论文不属于任何库，上链照样发生
        assert (await session.execute(select(LibraryPaper))).scalars().all() == []
        rows = (await session.execute(select(LLMUsage))).scalars().all()
        assert rows and all(r.library_id is None for r in rows)  # 记账落平台级，不摊给某个库
        assert any(r.stage == "extract" for r in rows)  # 概念定义那次调用记在触发人名下
        assert all(r.user_id is not None for r in rows)

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


async def test_daily_feed_plan_is_the_four_fixed_steps():
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
    assert obs["per_category"] == {"cs.AI": 2, "cs.CL": 1, "cs.CV": 0}
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
        entry.feed_date = entry.feed_date - dt.timedelta(days=8)
        await session.commit()

    obs = await get_action("daily.cleanup")(ctx, {})
    assert obs == {"expired": 1}

    # 开关默认关 → 跳过，且不算失败
    obs = await get_action("daily.embed")(ctx, {})
    assert obs == {"enabled": False, "embedded": 0, "failed": 0}
    assert "error" not in obs


async def test_daily_feed_voyage_end_to_end(client, monkeypatch):
    """整条任务跑通：四步依次 passed、run 到 done、论文真的入了池。"""
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

    async with get_sessionmaker()() as session:
        run = await daily_feed.create_daily_feed_voyage(session, created_by=None)
        run_id = run.id

    await VoyageEngine(event_bus=RecordingBus(), llm_router=LLMRouter()).run(run_id)

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
        ]
        assert all(s.status == "passed" for s in run.steps)

    token = await register_and_login(client, email="e2e@example.com")
    resp = await client.get(
        "/api/daily/papers", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.json()["total"] == 1


async def test_daily_fetch_reports_error_when_every_category_is_empty(client, monkeypatch):
    """全分类颗粒无收 = 抓取多半挂了（客户端把异常兜底成 []）→ 这步必须失败。"""
    from app.agents.voyage.actions import get_action
    from app.agents.voyage.checks import run_deterministic_checks
    from app.services import daily_feed

    await register_and_login(client)
    monkeypatch.setattr(daily_feed, "get_arxiv_client", lambda: _StubArxiv({}))

    ctx = _action_ctx()
    obs = await get_action("daily.fetch")(ctx, {})
    assert obs["fetched"] == 0
    assert obs["error"]
    # no_error 校验会判失败 → 任务进 paused_error（列表红色、可看日志、可重试）
    verdict, _ = run_deterministic_checks(
        [{"kind": "no_error"}], observation=obs, checkpoint=ctx.checkpoint
    )
    assert verdict is not None and verdict["passed"] is False
