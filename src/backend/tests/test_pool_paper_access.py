"""池级可见性（P5b 修复）：个人补充入库（零库成员行）的论文，
本人经书架 / 个人库可读（详情 / 子资源 / 个人 wiki 全链路）；
他人（未入架未收藏）与写库端点维持 404。"""

import uuid

import pytest

from app.core.db import get_sessionmaker
from app.models.paper import Paper
from tests.conftest import register_and_login

RECT = {"x0": 0.1, "y0": 0.1, "x1": 0.5, "y1": 0.12}


async def _setup(client, *, name="pool-proj", email="pool-alice@example.com"):
    token = await register_and_login(client, email=email)
    headers = {"Authorization": f"Bearer {token}"}
    resp = await client.post("/api/projects", json={"name": name}, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"], headers


async def _seed_pool_only_paper(**fields) -> str:
    async with get_sessionmaker()() as session:
        paper = Paper(**({"title": "Pool Access Paper", "source": "manual"} | fields))
        session.add(paper)
        await session.commit()
        return str(paper.id)


async def test_shelved_pool_paper_full_reading_chain(client):
    """个人补充论文（零库成员行）入架后：详情 / 笔记 / 划线 / 图片 /
    个人库埋点 / 个人 wiki 全链路对本人可用。"""
    project_id, headers = await _setup(client)
    paper_id = await _seed_pool_only_paper(
        title="Personally Supplemented", abstract="No library membership."
    )
    resp = await client.post(
        f"/api/projects/{project_id}/shelf", json={"paper_id": paper_id}, headers=headers
    )
    assert resp.status_code == 201, resp.text

    # 详情：course 上下文 = 入架课题；无判断字段但形状完整
    resp = await client.get(f"/api/papers/{paper_id}", headers=headers)
    assert resp.status_code == 200, resp.text
    detail = resp.json()
    assert detail["project_id"] == project_id
    assert detail["status"] == "included"
    assert detail["relevance_score"] is None
    assert detail["wiki_content"] is None and detail["has_wiki"] is False
    assert detail["concepts"] == []

    # 子资源：PDF（可见但无文件 → PDF_NOT_AVAILABLE 而非 PAPER_NOT_FOUND）/ 图片
    resp = await client.get(f"/api/papers/{paper_id}/pdf", headers=headers)
    assert resp.status_code == 404 and resp.json()["detail"] == "PDF_NOT_AVAILABLE"
    resp = await client.get(f"/api/papers/{paper_id}/figures", headers=headers)
    assert resp.status_code == 200 and resp.json() == []

    # 笔记 / 划线
    resp = await client.post(
        f"/api/papers/{paper_id}/notes", json={"content": "库外论文的笔记"}, headers=headers
    )
    assert resp.status_code == 201, resp.text
    resp = await client.get(f"/api/papers/{paper_id}/notes", headers=headers)
    assert [n["content"] for n in resp.json()] == ["库外论文的笔记"]
    resp = await client.post(
        f"/api/papers/{paper_id}/highlights",
        json={"page": 1, "rects": [RECT], "selected_text": "pool highlight"},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    resp = await client.get(f"/api/papers/{paper_id}/highlights", headers=headers)
    assert len(resp.json()) == 1

    # 个人状态 + 个人库埋点 / 收藏态
    resp = await client.put(
        f"/api/papers/{paper_id}/my-meta", json={"starred": True}, headers=headers
    )
    assert resp.status_code == 200 and resp.json()["starred"] is True
    resp = await client.post("/api/me/library/visits", json={"paper_id": paper_id}, headers=headers)
    assert resp.status_code == 201, resp.text
    resp = await client.get(f"/api/me/library/state?paper_id={paper_id}", headers=headers)
    assert resp.status_code == 200 and resp.json()["saved"] is True  # 入架已代收藏

    # 池内论文还没编译过 → 书架条目的解读为空（不再有个人版/快照兜底）
    resp = await client.get(f"/api/projects/{project_id}/shelf", headers=headers)
    assert resp.json()["items"][0]["wiki_content"] is None


async def test_pool_paper_visible_via_personal_library_after_shelf_removal(client):
    """移出书架后个人库条目仍在 → 仍可读，课题上下文为空。"""
    project_id, headers = await _setup(client, name="pool-proj-2", email="pool-bob@example.com")
    paper_id = await _seed_pool_only_paper(title="Entry Only Paper", arxiv_id="2405.00005")
    resp = await client.post(
        f"/api/projects/{project_id}/shelf", json={"paper_id": paper_id}, headers=headers
    )
    assert resp.status_code == 201
    resp = await client.delete(f"/api/projects/{project_id}/shelf/{paper_id}", headers=headers)
    assert resp.status_code == 204

    resp = await client.get(f"/api/papers/{paper_id}", headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["project_id"] is None  # 无课题上下文（仅个人库可达）
    resp = await client.get(f"/api/papers/{paper_id}/notes", headers=headers)
    assert resp.status_code == 200


async def test_pool_paper_hidden_from_others_and_write_paths(client):
    project_id, headers = await _setup(client, name="pool-proj-3", email="pool-carol@example.com")
    paper_id = await _seed_pool_only_paper(title="Private Chain Paper")
    resp = await client.post(
        f"/api/projects/{project_id}/shelf", json={"paper_id": paper_id}, headers=headers
    )
    assert resp.status_code == 201

    # 其他用户（未入架未收藏）：详情与子资源一律 404
    stranger = await register_and_login(client, email="pool-stranger@example.com")
    sh = {"Authorization": f"Bearer {stranger}"}
    for url in (
        f"/api/papers/{paper_id}",
        f"/api/papers/{paper_id}/notes",
        f"/api/papers/{paper_id}/highlights",
        f"/api/papers/{paper_id}/figures",
        f"/api/papers/{paper_id}/pdf",
    ):
        resp = await client.get(url, headers=sh)
        assert resp.status_code == 404, url

    # 写库成员行的端点不开池级兜底：对本人也维持 404（应走个人 wiki / 书架操作）
    resp = await client.patch(
        f"/api/papers/{paper_id}", json={"status": "excluded"}, headers=headers
    )
    assert resp.status_code == 404
    resp = await client.post(f"/api/papers/{paper_id}/recompile", headers=headers)
    assert resp.status_code == 404
    resp = await client.delete(f"/api/papers/{paper_id}", headers=headers)
    assert resp.status_code == 404
    async with get_sessionmaker()() as session:
        assert await session.get(Paper, uuid.UUID(paper_id)) is not None


async def test_daily_feed_paper_readable_without_collecting(client):
    """每日推送里的论文未被任何人收录时，任何登录用户仍可读（详情 / 阅读页取数）。

    回归：每日详情点「阅读原文」曾报 PAPER_NOT_FOUND——池级兜底只认书架 / 个人库，
    而未收录的每日论文两者都不在。
    """
    import datetime as dt

    from app.models.daily_feed import DailyFeedEntry

    paper_id = await _seed_pool_only_paper(title="Daily Only Paper", source="arxiv")
    async with get_sessionmaker()() as session:
        session.add(
            DailyFeedEntry(
                paper_id=uuid.UUID(paper_id),
                feed_date=dt.datetime.now(dt.UTC).date(),
                primary_category="cs.AI",
            )
        )
        await session.commit()

    # 任意登录用户（既没入架也没收藏）都能读到详情
    token = await register_and_login(client, email="daily-reader@example.com")
    resp = await client.get(f"/api/papers/{paper_id}", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["title"] == "Daily Only Paper"


async def test_pool_paper_not_in_daily_feed_stays_hidden(client):
    """不在每日推送、也没入架/收藏的池论文，对他人仍是 404（不放宽既有边界）。"""
    paper_id = await _seed_pool_only_paper(title="Nobody's Paper")
    token = await register_and_login(client, email="daily-stranger@example.com")
    resp = await client.get(f"/api/papers/{paper_id}", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 404


# ---- 工具层（PolarisBuddy / MCP）：每日新论文必须读得到，其余照旧按范围收紧 ----
#
# 与上面 HTTP 端点的兜底**故意不完全一致**：端点还放行书架 / 个人库可达的论文，工具
# 层不放行。工具压根没有渠道拿到那些 id（只有 search_daily_pool 会把库外论文交给模型），
# 跟着放开只会平白松掉课题范围。这一条的边界由 test_tools_registry 那边钉住。


async def _daily_only_paper(**fields) -> str:
    """造一篇只在每日新论文池里的论文：有 Paper 行，零库成员行。"""
    import datetime as dt

    from app.models.daily_feed import DailyFeedEntry

    paper_id = await _seed_pool_only_paper(**({"source": "arxiv"} | fields))
    async with get_sessionmaker()() as session:
        session.add(
            DailyFeedEntry(
                paper_id=uuid.UUID(paper_id),
                feed_date=dt.datetime.now(dt.UTC).date(),
                primary_category="cs.AI",
            )
        )
        await session.commit()
    return paper_id


async def _buddy_ctx(client, email: str):
    """全局助手的上下文：有身份，但一个库都搜不到（library_ids=()）。"""
    from sqlalchemy import select

    from app.core.llm.router import LLMRouter
    from app.models.user import User
    from app.tools import ToolContext

    await register_and_login(client, email=email)
    async with get_sessionmaker()() as session:
        user = (await session.execute(select(User).where(User.email == email))).scalar_one()
        user_id = user.id
    return ToolContext(project_id=None, llm=LLMRouter(), library_ids=(), user_id=user_id)


async def test_buddy_can_read_the_daily_papers_it_just_searched(client):
    """``search_daily_pool`` 搜出来的论文，``get_paper`` 必须读得到。

    线上现象：助手搜出 20 篇当日新论文，挨个 get_paper，20 篇全报「库内不存在该论文」，
    整条「今天有什么和我相关」的链路一步都走不动。根因是每日池按定义装的就是**还没
    进任何库**的论文，而工具层只认库成员行——这条链路必然 100% 失败，且论文就明明白白
    摆在用户眼前的每日新论文页上。同一篇论文 HTTP 端点早就读得到（见上面几条），是工具
    层的可见性判断没跟上。
    """
    import app.tools as tools

    ctx = await _buddy_ctx(client, "buddy-daily@example.com")
    paper_id = await _daily_only_paper(title="Daily Pool Paper", abstract="brand new today")

    found = await tools.run_tool(ctx, "search_daily_pool", {"limit": 20})
    assert paper_id in [p["paper_id"] for p in found["papers"]], found

    detail = await tools.run_tool(ctx, "get_paper", {"paper_id": paper_id})
    assert detail["title"] == "Daily Pool Paper"
    # 读得到，但别让模型把今天刚出的新论文说成「你库里的工作」
    assert detail["in_library"] is False


async def test_reading_tools_agree_with_get_paper_on_daily_papers(client):
    """能 get_paper 就能 read_wiki / read_fulltext：四个闸门是同一个，不能各判各的。

    #269 之后又栽过一次的老毛病——漏改一个模块，症状是某类工具在某种上下文里整体
    失灵，而其它工具一切正常。
    """
    import app.tools as tools

    ctx = await _buddy_ctx(client, "buddy-daily-read@example.com")
    paper_id = await _daily_only_paper(title="Daily Readable", abstract="no wiki, no fulltext")

    wiki = await tools.run_tool(ctx, "read_wiki", {"paper_id": paper_id})
    assert wiki["wiki"] is None and wiki["abstract"] == "no wiki, no fulltext"
    text = await tools.run_tool(ctx, "read_fulltext", {"paper_id": paper_id})
    assert text["text"] is None and text["abstract"] == "no wiki, no fulltext"


async def test_tools_still_refuse_a_paper_nobody_can_see(client):
    """兜底没有放宽边界：不在每日池、也没入架/收藏的池论文，工具照旧读不到。"""
    import app.tools as tools

    ctx = await _buddy_ctx(client, "buddy-stranger@example.com")
    paper_id = await _seed_pool_only_paper(title="Nobody's Paper For Tools")

    with pytest.raises(ValueError) as err:
        await tools.run_tool(ctx, "get_paper", {"paper_id": paper_id})
    # 报错要说清找过哪几处；「库内不存在」会让人以为论文丢了
    assert "读不到这篇论文" in str(err.value)


def test_no_tool_module_decides_paper_readability_on_its_own():
    """按 id 读单篇论文的可见性判断只能住在 scope.py 里。

    这条是上一条的结构性版本：#269 那次漏改了三个模块，这次漏改的是每日池兜底。
    症状都一样难联想——某类工具在某种上下文里整体失灵。与其指望下次记得，不如把
    「别自己判成员资格」钉死在测试里。
    """
    import pathlib

    tools_dir = pathlib.Path(__file__).resolve().parent.parent / "app" / "tools"
    offenders = [
        path.name
        for path in tools_dir.glob("*.py")
        if path.name != "scope.py" and "membership_in_scope" in path.read_text(encoding="utf-8")
    ]
    assert offenders == [], f"这些模块还在自己判论文成员资格：{offenders}"
