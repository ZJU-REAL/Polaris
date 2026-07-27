"""向量空间隔离：不同模型建的向量绝不参与同一次比较（issue #191）。

改造前的失效方式是**静默的**：查询向量与文档向量出自不同模型时，维度不同才会报错，
维度恰好相同时一路成功返回，只是排序全乱。所以这里的用例大多在验证「该拒绝的拒绝了」
「该看不见的看不见」，而不只是happy path。
"""

import uuid

import pytest
from sqlalchemy import select

from app.core.db import get_sessionmaker
from app.core.embedding_space import (
    EmbeddingSpace,
    EmbeddingSpaceMismatchError,
    active_space,
    set_active_space,
)
from app.core.llm.fake import EMBEDDING_DIM
from app.core.llm.router import GLOBAL_ONLY_STAGES, LLMRouter
from app.models.paper import new_paper
from app.models.vectors import PaperVector
from app.services.embedding import (
    adopt_routed_model,
    embed_documents,
    embed_query,
    papers_with_vector,
    space_inventory,
    upsert_paper_vector,
)
from tests.conftest import add_paper, make_project_with_library, register_and_login
from tests.vector_helpers import FAKE_SPACE, ensure_space, get_paper_vector, set_paper_vector

OTHER_SPACE = EmbeddingSpace(model="some-other-model", dim=EMBEDDING_DIM)


async def _pool_paper(session, title="Vector Owner") -> uuid.UUID:
    """建一篇最简内容池论文（向量行有外键，不能挂在凭空的 id 上）。"""
    paper = new_paper(title=title)
    session.add(paper)
    await session.flush()
    return paper.id


# ---- 1. 空间标识与首次初始化 ----


async def test_first_embed_defines_the_space_from_the_model(client):
    """维度不写死：平台第一次嵌入时按模型实际返回的维度定义激活空间。"""
    await register_and_login(client)
    async with get_sessionmaker()() as session:
        assert await active_space(session) is None  # 空库：还没有空间

        vectors, space = await embed_documents(session, ["hello"])
        await session.commit()

        assert len(vectors[0]) == space.dim  # 空间维度 = 模型实际返回的维度
        assert space.key == f"{space.model}@{space.dim}"
        assert await active_space(session) == space  # 已持久化


async def test_vectors_carry_their_space(client):
    """每条向量都记着自己出自哪个模型、多少维——检索据此过滤。"""
    await register_and_login(client)
    async with get_sessionmaker()() as session:
        space = await ensure_space(session)
        paper_id = await _pool_paper(session)
        await upsert_paper_vector(session, paper_id, [0.0] * space.dim, space)
        await session.commit()

        stored = (
            await session.execute(select(PaperVector).where(PaperVector.paper_id == paper_id))
        ).scalar_one()
        assert stored.space == space.key
        assert stored.dim == space.dim
        assert stored.model == space.model
        assert stored.built_at is not None


# ---- 2. 写入侧：维度/模型不符一律拒绝 ----


async def test_wrong_dimension_is_refused_not_stored(client):
    """维度对不上就抛错，绝不入库——sqlite 的 JSON 列本来会照单全收。"""
    await register_and_login(client)
    async with get_sessionmaker()() as session:
        space = await ensure_space(session)
        paper_id = await _pool_paper(session)
        with pytest.raises(EmbeddingSpaceMismatchError):
            await upsert_paper_vector(session, paper_id, [0.0] * (space.dim + 1), space)


async def test_model_change_blocks_writes_until_adopted(client, monkeypatch):
    """换了嵌入模型但没确认 → 拒绝写入，而不是把两个模型的向量混进一个池子。"""
    await register_and_login(client)
    async with get_sessionmaker()() as session:
        await set_active_space(session, OTHER_SPACE)  # 假装存量向量出自别的模型
        await session.commit()

        with pytest.raises(EmbeddingSpaceMismatchError) as exc:
            await embed_documents(session, ["hello"])
        assert OTHER_SPACE.model in str(exc.value)


async def test_adopt_switches_space_and_keeps_old_vectors(client):
    """确认换用新模型：激活空间切过去，旧向量原样留在库里（可切回）。"""
    await register_and_login(client)
    async with get_sessionmaker()() as session:
        paper_id = await _pool_paper(session)
        await set_active_space(session, OTHER_SPACE)
        await session.commit()
        await upsert_paper_vector(session, paper_id, [0.25] * OTHER_SPACE.dim, OTHER_SPACE)
        await session.commit()

        space, previous = await adopt_routed_model(session)
        assert previous == OTHER_SPACE.key
        assert space.key != OTHER_SPACE.key
        assert await active_space(session) == space

        # 旧向量还在，只是当前空间下查不到它
        assert await papers_with_vector(session, [paper_id], space) == set()
        assert await papers_with_vector(session, [paper_id], OTHER_SPACE) == {paper_id}


# ---- 3. 读取侧：跨空间的向量看不见 ----


async def test_lookups_never_cross_spaces(client):
    """同一篇论文在两个空间各有一条向量，查哪个空间就只拿到哪一条。"""
    await register_and_login(client)
    async with get_sessionmaker()() as session:
        await ensure_space(session)
        paper_id = await _pool_paper(session)
        await upsert_paper_vector(session, paper_id, [0.1] * FAKE_SPACE.dim, FAKE_SPACE)
        await upsert_paper_vector(session, paper_id, [0.9] * OTHER_SPACE.dim, OTHER_SPACE)
        await session.commit()

        assert await get_paper_vector(session, paper_id) == pytest.approx([0.1] * FAKE_SPACE.dim)
        assert await papers_with_vector(session, [paper_id], OTHER_SPACE) == {paper_id}


async def test_query_embedding_needs_a_space(client):
    """平台一条向量都没建过时语义检索直接降级，不会拿无归属的向量去比。"""
    await register_and_login(client)
    async with get_sessionmaker()() as session:
        with pytest.raises(NotImplementedError):
            await embed_query(session, "anything")


async def test_model_change_degrades_search_instead_of_erroring(client):
    """换了模型没确认时，语义检索降级到关键词，而不是 500。

    读路径统一按 NotImplementedError 降级；只有写路径才把不一致当成硬错误。
    """
    token = await register_and_login(client)
    headers = {"Authorization": f"Bearer {token}"}
    project_id, _ = await make_project_with_library(client, headers, name="degrade")
    async with get_sessionmaker()() as session:
        await add_paper(
            session,
            project_id=uuid.UUID(project_id),
            title="Searchable Paper",
            doi="10.1/degrade",
            status="compiled",
        )
        await session.commit()
        await set_active_space(session, OTHER_SPACE)  # 现有向量出自别的模型
        await session.commit()

        with pytest.raises(NotImplementedError):
            await embed_query(session, "anything")

    resp = await client.get(
        f"/api/projects/{project_id}/search",
        params={"q": "Searchable", "mode": "semantic"},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["mode_used"] == "keyword", "该降级而不是报错"


async def test_index_status_marks_stale_instead_of_missing(client):
    """换模型后论文状态是「待重建」而不是「未构建」——不能让人以为数据丢了。"""
    token = await register_and_login(client)
    headers = {"Authorization": f"Bearer {token}"}
    project_id, _ = await make_project_with_library(client, headers, name="stale")
    async with get_sessionmaker()() as session:
        paper = await add_paper(
            session, project_id=uuid.UUID(project_id), title="Stale Vector", doi="10.1/stale"
        )
        await session.commit()
        paper_id = paper.id
        await set_paper_vector(session, paper_id)  # 建在 FAKE_SPACE 里

    body = (await client.get(f"/api/papers/{paper_id}/index-status", headers=headers)).json()
    assert body["paper_vector"]["built"] is True
    assert body["paper_vector"]["stale"] is False

    # 管理员换了模型
    async with get_sessionmaker()() as session:
        await set_active_space(session, OTHER_SPACE)
        await session.commit()

    body = (await client.get(f"/api/papers/{paper_id}/index-status", headers=headers)).json()
    assert body["paper_vector"]["built"] is False, "旧空间的向量不算已建"
    assert body["paper_vector"]["stale"] is True, "但也不是从没建过"
    assert body["paper_vector"]["model"] == FAKE_SPACE.model  # 还能看出是谁建的


# ---- 4. 嵌入模型不给用户自选 ----


async def test_embedding_stage_ignores_user_routes(client):
    """自管用户的 embedding 路由不生效：向量是全平台共享的一份数据。"""
    assert "embedding" in GLOBAL_ONLY_STAGES
    router = LLMRouter()
    owner = await router._effective_owner(uuid.uuid4(), "embedding")
    assert owner is None, "嵌入一律走全局配置"
    # 对照：普通环节仍按用户自管状态解析
    assert await router._effective_owner(None, "default") is None


async def test_user_cannot_configure_embedding_route(client):
    """个人路由表拒收 embedding 环节（填了也不会生效，所以直接报错而不是静默丢弃）。"""
    token = await register_and_login(client)
    headers = {"Authorization": f"Bearer {token}"}
    await client.post("/api/me/llm/self-manage", headers=headers)
    resp = await client.post(
        "/api/me/llm/providers",
        json={"name": "mine", "kind": "fake", "base_url": None, "api_key": "x"},
        headers=headers,
    )
    provider_id = resp.json()["id"]

    resp = await client.put(
        "/api/me/llm/routes",
        json=[{"stage": "embedding", "provider_id": provider_id, "model": "my-embed"}],
        headers=headers,
    )
    assert resp.status_code == 400
    assert "embedding" in resp.json()["detail"]

    # rerank 不受影响：逐条打分，没有跨调用可比性问题
    resp = await client.put(
        "/api/me/llm/routes",
        json=[{"stage": "rerank", "provider_id": provider_id, "model": "my-rerank"}],
        headers=headers,
    )
    assert resp.status_code == 200, resp.text


# ---- 5. 管理端 ----


async def test_admin_space_status_reports_mismatch(client):
    """管理端如实报告「路由表配的模型 ≠ 现有向量的模型」。"""
    token = await register_and_login(client)  # 首个用户 = admin
    headers = {"Authorization": f"Bearer {token}"}
    async with get_sessionmaker()() as session:
        await set_active_space(session, OTHER_SPACE)
        paper_id = await _pool_paper(session)
        await session.commit()
        await upsert_paper_vector(session, paper_id, [0.1] * OTHER_SPACE.dim, OTHER_SPACE)
        await session.commit()

    body = (await client.get("/api/admin/settings/embedding-space", headers=headers)).json()
    assert body["active"]["model"] == OTHER_SPACE.model
    assert body["active"]["papers"] == 1
    assert body["mismatched"] is True, "配的模型已经不是建库那个了"

    resp = await client.post("/api/admin/settings/embedding-space/adopt", headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["previous"] == OTHER_SPACE.key

    body = (await client.get("/api/admin/settings/embedding-space", headers=headers)).json()
    assert body["mismatched"] is False
    # 旧那批向量还在库里，只是不再是激活的
    keys = {s["key"]: s["active"] for s in body["spaces"]}
    assert keys[OTHER_SPACE.key] is False


async def test_space_inventory_counts_each_kind(client):
    await register_and_login(client)
    async with get_sessionmaker()() as session:
        await ensure_space(session)
        for i in range(2):
            paper_id = await _pool_paper(session, title=f"Counted {i}")
            await upsert_paper_vector(session, paper_id, [0.0] * FAKE_SPACE.dim, FAKE_SPACE)
        await session.commit()
        items = await space_inventory(session)
    assert len(items) == 1
    assert items[0]["papers"] == 2 and items[0]["active"] is True
