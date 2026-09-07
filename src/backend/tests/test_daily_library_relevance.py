"""每日论文池按用户文献库派生相关性（#623）。

覆盖：质心计算与指纹缓存/失效、相关性×新近度融合排序（fake 向量，确定性）、
无库时与按时间完全一致、无向量库的关键词降级、列表条目的「与你的库相关」徽章数据。
"""

import datetime as dt
import uuid

import pytest
from sqlalchemy import select

from app.core.db import get_sessionmaker
from app.core.llm.fake import EMBEDDING_DIM
from app.models.daily_feed import DailyFeedEntry
from app.models.library_direction import DirectionLibrary
from app.models.system_setting import SystemSetting
from app.models.user import User
from app.services import daily_relevance
from tests.conftest import add_paper, ensure_project_library, register_and_login
from tests.vector_helpers import set_paper_vector

pytestmark = pytest.mark.asyncio


def _vec(axis: int) -> list[float]:
    """单位基向量：不同 axis 互相正交（余弦 0），同 axis 余弦 1——分数完全可控。"""
    v = [0.0] * EMBEDDING_DIM
    v[axis] = 1.0
    return v


def _today() -> dt.date:
    return dt.datetime.now(dt.UTC).date()


async def _setup_library(client, *, member_axes: list[int]):
    """注册（首个用户）→ 建课题（隐式建库）→ 塞成员论文并落向量。"""
    token = await register_and_login(client)
    headers = {"Authorization": f"Bearer {token}"}
    resp = await client.post(
        "/api/projects",
        json={"name": "agents", "statement": "LLM agent planning"},
        headers=headers,
    )
    assert resp.status_code in (200, 201), resp.text
    project_id = uuid.UUID(resp.json()["id"])
    async with get_sessionmaker()() as session:
        for i, axis in enumerate(member_axes):
            await add_paper(
                session,
                project_id=project_id,
                title=f"member {i}",
                status="scored",
                embedding=_vec(axis),
            )
        await session.commit()
    libs = (await client.get("/api/libraries", headers=headers)).json()
    rows = libs if isinstance(libs, list) else libs["items"]
    return headers, uuid.UUID(rows[0]["id"]), rows[0]["name"]


async def _add_entry(
    title: str,
    *,
    axis: int | None = None,
    feed_date: dt.date | None = None,
    abstract: str | None = None,
) -> uuid.UUID:
    """直接塞一条每日池条目（可选带论文向量）。"""
    from app.models.paper import new_paper

    async with get_sessionmaker()() as session:
        paper = new_paper(
            source="arxiv", dedup_key=uuid.uuid4().hex, title=title, abstract=abstract
        )
        session.add(paper)
        await session.flush()
        session.add(
            DailyFeedEntry(
                paper_id=paper.id,
                feed_date=feed_date or _today(),
                primary_category="cs.AI",
            )
        )
        await session.commit()
        paper_id = paper.id
    if axis is not None:
        async with get_sessionmaker()() as session:
            await set_paper_vector(session, paper_id, _vec(axis))
    return paper_id


async def _first_user() -> User:
    async with get_sessionmaker()() as session:
        return (await session.execute(select(User).limit(1))).scalars().one()


# ---- 质心：计算 + 指纹缓存 + 失效重算 ----


async def test_centroid_cached_and_invalidated(client):
    _headers, library_id, _name = await _setup_library(client, member_axes=[0, 0])
    user = await _first_user()

    async with get_sessionmaker()() as session:
        anchors = await daily_relevance.library_anchors(session, user=user)
        assert len(anchors) == 1
        assert anchors[0].centroid is not None
        assert anchors[0].centroid[0] == pytest.approx(1.0)

        # 缓存行已落 system_settings
        row = await session.get(SystemSetting, f"daily_library_anchor:{library_id}")
        assert row is not None and row.value["centroid"][0] == pytest.approx(1.0)

    # 指纹没变时读缓存：篡改缓存值，再读必须读到篡改后的（证明没有重算）
    async with get_sessionmaker()() as session:
        row = await session.get(SystemSetting, f"daily_library_anchor:{library_id}")
        tampered = _vec(5)
        row.value = dict(row.value, centroid=tampered)
        await session.commit()
    async with get_sessionmaker()() as session:
        anchors = await daily_relevance.library_anchors(session, user=user)
        assert anchors[0].centroid[5] == pytest.approx(1.0)

    # 库一变（再收一篇）指纹变 → 自动重算，篡改值被真质心覆盖
    async with get_sessionmaker()() as session:
        project_id = (
            (await session.execute(select(DirectionLibrary.project_id))).scalars().one()
        )
        await add_paper(
            session, project_id=project_id, title="member 2", status="scored", embedding=_vec(1)
        )
        await session.commit()
    async with get_sessionmaker()() as session:
        anchors = await daily_relevance.library_anchors(session, user=user)
        centroid = anchors[0].centroid
        assert centroid[0] == pytest.approx(2 / 3)
        assert centroid[1] == pytest.approx(1 / 3)
        assert centroid[5] == pytest.approx(0.0)


# ---- 相关性排序 + 徽章 ----


async def test_relevance_sort_orders_related_first_and_badges(client):
    _headers, library_id, library_name = await _setup_library(client, member_axes=[0, 0])
    related = await _add_entry("Related agent paper", axis=0)
    unrelated = await _add_entry("Unrelated topology paper", axis=7)

    headers = _headers
    resp = await client.get("/api/daily/papers?sort=relevance&size=10", headers=headers)
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    ids = [row["paper_id"] for row in items]
    assert ids == [str(related), str(unrelated)]

    by_id = {row["paper_id"]: row for row in items}
    hit = by_id[str(related)]
    assert hit["related_library_id"] == str(library_id)
    assert hit["related_library_name"] == library_name
    # 正交向量余弦 0，低于阈值 → 不贴徽章
    assert by_id[str(unrelated)]["related_library_id"] is None
    assert by_id[str(unrelated)]["related_library_name"] is None


async def test_badges_present_even_when_sorted_by_date(client):
    _headers, _library_id, library_name = await _setup_library(client, member_axes=[0])
    related = await _add_entry("Related agent paper", axis=0)

    resp = await client.get("/api/daily/papers?sort=date&size=10", headers=_headers)
    assert resp.status_code == 200
    by_id = {row["paper_id"]: row for row in resp.json()["items"]}
    assert by_id[str(related)]["related_library_name"] == library_name


async def test_fusion_is_gentle_recency_still_matters(client):
    """权重 0.3 的对账：昨天之前的高相关能压过今天的无关，但太旧就压不过。

    fused = 0.7 × recency + 0.3 × relevance，窗口 14 天：
    - 3 天前的相关：0.7×(1-3/14) + 0.3 = 0.85
    - 今天的无关：  0.7×1              = 0.70
    - 7 天前的相关：0.7×(1-7/14) + 0.3 = 0.65
    """
    _headers, _library_id, _name = await _setup_library(client, member_axes=[0])
    today = _today()
    mid_related = await _add_entry(
        "Mid related", axis=0, feed_date=today - dt.timedelta(days=3)
    )
    new_unrelated = await _add_entry("New unrelated", axis=7, feed_date=today)
    old_related = await _add_entry(
        "Old related", axis=0, feed_date=today - dt.timedelta(days=7)
    )

    resp = await client.get("/api/daily/papers?sort=relevance&size=10", headers=_headers)
    assert resp.status_code == 200
    ids = [row["paper_id"] for row in resp.json()["items"]]
    assert ids == [str(mid_related), str(new_unrelated), str(old_related)]


# ---- 无库降级：与按时间完全一致 ----


async def test_relevance_without_libraries_matches_date_sort(client):
    token = await register_and_login(client)  # 不建课题：一个库都没有
    headers = {"Authorization": f"Bearer {token}"}
    today = _today()
    await _add_entry("Yesterday paper", feed_date=today - dt.timedelta(days=1))
    await _add_entry("Today paper", feed_date=today)

    by_rel = await client.get("/api/daily/papers?sort=relevance&size=10", headers=headers)
    by_date = await client.get("/api/daily/papers?sort=date&size=10", headers=headers)
    assert by_rel.status_code == by_date.status_code == 200
    rel_ids = [row["entry_id"] for row in by_rel.json()["items"]]
    date_ids = [row["entry_id"] for row in by_date.json()["items"]]
    assert rel_ids == date_ids
    # 徽章字段存在但为空：没有库就没有「与你的库相关」
    assert all(row["related_library_id"] is None for row in by_rel.json()["items"])


# ---- 无向量库：关键词降级 ----


async def test_keyword_fallback_when_library_has_no_vectors(client):
    token = await register_and_login(client)
    headers = {"Authorization": f"Bearer {token}"}
    resp = await client.post(
        "/api/projects",
        json={"name": "diffusion", "statement": "diffusion models"},
        headers=headers,
    )
    project_id = uuid.UUID(resp.json()["id"])
    # 库空着（没有成员向量），只有收录关键词。建课题不建库，库随首次写入才出现，
    # 这里直接用测试助手把隐式库建出来再填关键词。
    async with get_sessionmaker()() as session:
        library = await ensure_project_library(session, project_id)
        library.definition = dict(
            library.definition or {}, keywords={"include": ["diffusion"]}
        )
        library_name = library.name
        await session.commit()

    matched = await _add_entry("Diffusion transformers at scale")
    other = await _add_entry("Sparse graph pruning")

    resp = await client.get("/api/daily/papers?sort=relevance&size=10", headers=headers)
    assert resp.status_code == 200
    items = resp.json()["items"]
    ids = [row["paper_id"] for row in items]
    assert ids == [str(matched), str(other)]
    by_id = {row["paper_id"]: row for row in items}
    assert by_id[str(matched)]["related_library_name"] == library_name
    assert by_id[str(other)]["related_library_name"] is None
