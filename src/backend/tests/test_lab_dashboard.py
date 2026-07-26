"""实验室数据面板（/lab）：索引统计 / 用量排行榜 / 跨库图谱。

- 统计跨库去重：同一篇论文进两个库只算一次；
- 统计按可见性收敛：普通用户看不到别人个人库的任何计数；
- 排行榜的管理员开关：关掉后普通用户 403、管理员照常；
- 图谱：不传 library_id = 全部可见库的并集，传了只看那一个库。
"""

import uuid

from sqlalchemy import select

from app.core.db import get_sessionmaker
from app.models.library_direction import DirectionLibrary, LibraryPaper
from app.models.llm_config import LLMUsage
from app.models.paper import Concept, Paper, PaperChunk, paper_concepts
from app.models.user import User
from tests.conftest import register_and_login


async def _hdr(client, email):
    return {"Authorization": f"Bearer {await register_and_login(client, email=email)}"}


async def _user_id(email: str) -> uuid.UUID:
    async with get_sessionmaker()() as session:
        row = (await session.execute(select(User).where(User.email == email))).scalar_one()
        return row.id


async def _make_library(*, name, is_public, owner_email=None) -> uuid.UUID:
    """直接建一个 active 库（不经课题），返回库 id。"""
    async with get_sessionmaker()() as session:
        submitted_by = None
        if owner_email is not None:
            submitted_by = (
                await session.execute(select(User).where(User.email == owner_email))
            ).scalar_one().id
        library = DirectionLibrary(
            name=name, status="active", is_public=is_public, submitted_by=submitted_by
        )
        session.add(library)
        await session.commit()
        return library.id


async def _add_member(
    *, library_ids, title, status="compiled", paper_id=None, chunks=0, embedded=False
) -> uuid.UUID:
    """建（或复用）一篇内容池论文并登记到若干库；可选顺带造全文分段与向量。"""
    async with get_sessionmaker()() as session:
        if paper_id is None:
            paper = Paper(title=title, embedding=[0.1] * 4 if embedded else None)
            session.add(paper)
            await session.flush()
            for seq in range(chunks):
                session.add(
                    PaperChunk(
                        paper_id=paper.id,
                        seq=seq,
                        text=f"{title} chunk {seq}",
                        embedding=[0.1] * 4 if embedded else None,
                    )
                )
            paper_id = paper.id
        for library_id in library_ids:
            session.add(
                LibraryPaper(library_id=library_id, paper_id=paper_id, status=status)
            )
        await session.commit()
        return paper_id


async def _add_concept(*, name, slug, paper_id=None) -> uuid.UUID:
    """建**已转正**的概念（全平台一份）；给了 paper_id 就挂到那篇论文上（库作用域由论文推导）。

    候选概念不对用户可见，也就不进这些统计/图谱，故造数据一律直接给 active。"""
    async with get_sessionmaker()() as session:
        concept = Concept(name=name, slug=slug, category="method", status="active")
        session.add(concept)
        await session.flush()
        if paper_id is not None:
            await session.execute(
                paper_concepts.insert().values(paper_id=paper_id, concept_id=concept.id)
            )
        await session.commit()
        return concept.id


async def _promote_admin(email: str) -> None:
    async with get_sessionmaker()() as session:
        user = (await session.execute(select(User).where(User.email == email))).scalar_one()
        user.role = "admin"
        await session.commit()


# ---- 统计 ----


async def test_lab_stats_dedupes_papers_across_libraries(client):
    """同一篇论文进两个公共库：库数 2，论文只算 1 篇（前端加总会错算成 2）。"""
    headers = await _hdr(client, "lab-a@example.com")  # 首个用户 = admin
    lib_a = await _make_library(name="方向甲", is_public=True)
    lib_b = await _make_library(name="方向乙", is_public=True)
    shared = await _add_member(library_ids=[lib_a], title="共享论文", chunks=2, embedded=True)
    await _add_member(library_ids=[lib_b], title="共享论文", paper_id=shared)
    # 有全文分段但还没建向量的一篇：用来区分「已分段」和「已嵌入」
    await _add_member(library_ids=[lib_b], title="只在乙库", status="scored", chunks=1)

    resp = await client.get("/api/lab/stats", headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["libraries"] == {"total": 2, "public": 2, "personal": 0}
    # 共享论文跨两个库只算一次 + 只在乙库那篇 = 2
    assert body["papers"]["library_members_deduped"] == 2
    assert body["papers"]["compiled"] == 1  # scored 那篇还没编译
    assert body["papers"]["pool_total"] == 2  # 内容池里也只有两条
    # 分段与向量同样按去重后的论文集合算
    assert body["chunks"]["papers_with_chunks"] == 2
    assert body["chunks"]["total_chunks"] == 3
    assert body["chunks"]["chunks_with_embedding"] == 2  # 未建向量那篇的分段不算
    assert body["chunks"]["vector_search_supported"] is False  # 测试跑 sqlite
    assert body["vectors"] == {"papers_with_embedding": 1, "papers_total": 2}


async def test_lab_stats_excludes_other_users_personal_library(client):
    """普通用户看不到别人个人库的论文/概念计数；库归属人自己看得到。"""
    owner = await _hdr(client, "lab-owner@example.com")  # 首个用户 = admin
    stranger = await _hdr(client, "lab-stranger@example.com")
    public_lib = await _make_library(name="公共方向", is_public=True)
    personal_lib = await _make_library(
        name="私人方向", is_public=False, owner_email="lab-stranger@example.com"
    )
    public_paper = await _add_member(library_ids=[public_lib], title="公开论文")
    personal_paper = await _add_member(library_ids=[personal_lib], title="私人论文")
    # 概念全平台一份；「哪个库有它」按库内论文的关联推导，故挂到各自库的论文上
    await _add_concept(name="Agent", slug="agent", paper_id=public_paper)
    await _add_concept(name="Secret", slug="secret", paper_id=personal_paper)

    # 第三方（既非归属人也非管理员）：只看得到公共库那一份
    third = await _hdr(client, "lab-third@example.com")
    body = (await client.get("/api/lab/stats", headers=third)).json()
    assert body["libraries"] == {"total": 1, "public": 1, "personal": 0}
    assert body["papers"]["library_members_deduped"] == 1
    assert body["concepts"]["total"] == 1

    # 归属人自己：公共库 + 自己的个人库
    body = (await client.get("/api/lab/stats", headers=stranger)).json()
    assert body["libraries"] == {"total": 2, "public": 1, "personal": 1}
    assert body["papers"]["library_members_deduped"] == 2
    assert body["concepts"]["total"] == 2

    # 管理员：全部
    body = (await client.get("/api/lab/stats", headers=owner)).json()
    assert body["libraries"]["total"] == 2
    assert body["papers"]["library_members_deduped"] == 2


# ---- 用量与排行榜 ----


async def _add_usage(user_id: uuid.UUID, *, prompt: int, completion: int) -> None:
    async with get_sessionmaker()() as session:
        session.add(
            LLMUsage(
                user_id=user_id,
                stage="score",
                model="fake-model",
                prompt_tokens=prompt,
                completion_tokens=completion,
            )
        )
        await session.commit()


async def test_lab_usage_visible_to_all_members(client):
    admin = await _hdr(client, "lab-usage-admin@example.com")
    member = await _hdr(client, "lab-usage-member@example.com")
    await _add_usage(await _user_id("lab-usage-admin@example.com"), prompt=100, completion=20)

    for headers in (admin, member):
        resp = await client.get("/api/lab/usage?days=30", headers=headers)
        assert resp.status_code == 200, resp.text
        rows = resp.json()
        assert len(rows) == 1
        assert rows[0]["prompt_tokens"] == 100 and rows[0]["completion_tokens"] == 20


async def test_leaderboard_ranks_by_tokens(client):
    admin = await _hdr(client, "lab-lb-admin@example.com")
    await _hdr(client, "lab-lb-small@example.com")
    await _hdr(client, "lab-lb-big@example.com")
    await _add_usage(await _user_id("lab-lb-small@example.com"), prompt=10, completion=5)
    await _add_usage(await _user_id("lab-lb-big@example.com"), prompt=900, completion=100)

    resp = await client.get("/api/lab/usage/leaderboard?days=30&limit=10", headers=admin)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["enabled"] is True and body["days"] == 30
    # 用量多的在前；没有任何记账的用户不出现
    assert [row["tokens_used"] for row in body["items"]] == [1000, 15]


async def test_leaderboard_switch_admin_and_member(client):
    """开关 × 角色 四种组合：关掉后只有管理员看得到。"""
    admin = await _hdr(client, "lab-sw-admin@example.com")
    member = await _hdr(client, "lab-sw-member@example.com")

    # 默认开：两边都能看
    assert (await client.get("/api/lab/usage/leaderboard", headers=admin)).status_code == 200
    assert (await client.get("/api/lab/usage/leaderboard", headers=member)).status_code == 200

    # 管理员关掉
    resp = await client.put(
        "/api/admin/settings/lab-leaderboard", json={"enabled": False}, headers=admin
    )
    assert resp.status_code == 200 and resp.json()["enabled"] is False
    # 普通用户改不了这个开关
    assert (
        await client.put(
            "/api/admin/settings/lab-leaderboard", json={"enabled": True}, headers=member
        )
    ).status_code == 403

    # 关掉后：普通用户 403，管理员仍 200（带 enabled=false 提示当前是关的）
    resp = await client.get("/api/lab/usage/leaderboard", headers=member)
    assert resp.status_code == 403 and resp.json()["detail"] == "LEADERBOARD_DISABLED"
    resp = await client.get("/api/lab/usage/leaderboard", headers=admin)
    assert resp.status_code == 200 and resp.json()["enabled"] is False

    # 概况页据此决定要不要显示这一区
    stats = (await client.get("/api/lab/stats", headers=member)).json()
    assert stats["leaderboard_enabled"] is False

    # 再打开：普通用户恢复
    await client.put("/api/admin/settings/lab-leaderboard", json={"enabled": True}, headers=admin)
    assert (await client.get("/api/lab/usage/leaderboard", headers=member)).status_code == 200


# ---- 跨库图谱 ----


async def test_lab_graph_unions_visible_libraries(client):
    headers = await _hdr(client, "lab-graph@example.com")
    lib_a = await _make_library(name="图谱甲", is_public=True)
    lib_b = await _make_library(name="图谱乙", is_public=True)
    paper_a = await _add_member(library_ids=[lib_a], title="甲库论文")
    paper_b = await _add_member(library_ids=[lib_b], title="乙库论文")
    await _add_concept(name="Agent", slug="agent", paper_id=paper_a)
    await _add_concept(name="Planner", slug="planner", paper_id=paper_b)

    # 不传 library_id：两个库的并集
    resp = await client.get("/api/lab/graph", headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    labels = {n["label"] for n in body["nodes"]}
    assert {"甲库论文", "乙库论文", "Agent", "Planner"} <= labels
    assert body["paper_total"] == 2
    assert body["truncated"] is False

    # 传 library_id：只看那一个库
    body = (await client.get(f"/api/lab/graph?library_id={lib_a}", headers=headers)).json()
    labels = {n["label"] for n in body["nodes"]}
    assert "甲库论文" in labels and "乙库论文" not in labels
    assert body["paper_total"] == 1


async def test_lab_graph_skips_invisible_library(client):
    await _hdr(client, "lab-graph-admin@example.com")  # 首个用户 = admin，占位
    owner = await _hdr(client, "lab-graph-owner@example.com")
    stranger = await _hdr(client, "lab-graph-stranger@example.com")
    personal = await _make_library(
        name="私人图谱", is_public=False, owner_email="lab-graph-owner@example.com"
    )
    await _add_member(library_ids=[personal], title="私人图谱论文")

    # 陌生人：并集里没有这个库；点名要也 404
    body = (await client.get("/api/lab/graph", headers=stranger)).json()
    assert body["nodes"] == [] and body["paper_total"] == 0
    resp = await client.get(f"/api/lab/graph?library_id={personal}", headers=stranger)
    assert resp.status_code == 404

    # 归属人自己看得到
    body = (await client.get("/api/lab/graph", headers=owner)).json()
    assert {n["label"] for n in body["nodes"]} >= {"私人图谱论文"}
