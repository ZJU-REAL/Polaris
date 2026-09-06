"""方法库 + purpose–mechanism 双索引（#663）：schema 归一化、双轴索引、检索语义、端点。

排序断言全靠 fake provider 的确定性：fake embedding 是词袋哈希向量
（core/llm/fake.py::fake_embedding），词面重叠 → 余弦相似，测试据此用轴文本的
用词控制两根轴的远近。
"""

import uuid

from sqlalchemy import select

from app.core.db import get_sessionmaker
from app.models.paper import Paper
from app.models.paper_extraction import PaperExtraction
from app.models.vectors import MethodVector
from app.services import paper_enrich
from app.services.extraction.runtime import extract_paper, normalize_payload
from app.services.extraction.schemas import METHOD_SCHEMA, get_schema, list_schemas
from app.services.method_index import refresh_paper_method_index, search_methods
from tests.conftest import add_paper, make_project_with_library, register_and_login

FULL_TEXT = """Method Probe Paper

Introduction

We probe how the method schema behaves under deterministic extraction.

Method

A deterministic mechanism section with enough prose to look like a paper.
"""


async def _noop_emit(stage, status, detail=None):  # noqa: ARG001
    return None


def _write_fulltext(tmp_path, text=FULL_TEXT):
    path = tmp_path / f"{uuid.uuid4().hex}.txt"
    path.write_text(text, encoding="utf-8")
    return str(path)


async def _method_vectors_of(paper_id):
    async with get_sessionmaker()() as session:
        rows = (
            (
                await session.execute(
                    select(MethodVector).where(MethodVector.paper_id == paper_id)
                )
            )
            .scalars()
            .all()
        )
        return sorted(r.axis for r in rows)


async def _add_method_paper(
    session, project_id, *, title, purpose, mechanism, tmp_path=None, **payload_extra
):
    """建一篇库内（scored）论文 + 手写 method@1 产物 + 刷双轴索引，返回 paper。"""
    paper = await add_paper(
        session, project_id=uuid.UUID(project_id), title=title, status="scored"
    )
    payload = {"purpose": purpose, **payload_extra}
    if mechanism is not None:
        payload["mechanism"] = mechanism
    session.add(
        PaperExtraction(paper_id=paper.id, schema_id="method", payload=payload)
    )
    await session.commit()
    await refresh_paper_method_index(session, paper)
    await session.commit()
    return paper


# ---- 1. schema 注册与归一化 ----


def test_method_schema_registered():
    schema = get_schema("method")
    assert schema is METHOD_SCHEMA
    assert schema.version == 1
    assert schema.stage == "extract_method"
    assert [f.name for f in schema.fields] == [
        "purpose",
        "mechanism",
        "baseline",
        "dataset",
        "protocol",
    ]
    assert schema in list_schemas()
    prompt = schema.system_prompt()
    assert "POLARIS_EXTRACT_METHOD" in prompt
    for field in schema.fields:
        assert f'"{field.name}"' in prompt


def test_method_normalize_caps():
    raw = {
        "purpose": "目" * 500,  # 超长截断到 400
        "mechanism": "   ",  # 空白 → 不入 payload
        "baseline": [f"基线{i}" for i in range(7)],  # 超过 max_items=5 → 截掉
        "dataset": ["ds-A", "ds-A", ""],  # 去重去空
        "protocol": "流" * 700,  # 截断到 600
        "hallucinated": "白名单外的键必须被丢掉",
    }
    payload, _ = normalize_payload(METHOD_SCHEMA, raw)
    assert set(payload) == {"purpose", "baseline", "dataset", "protocol"}
    assert len(payload["purpose"]) == 400
    assert len(payload["baseline"]) == 5
    assert payload["dataset"] == ["ds-A"]
    assert len(payload["protocol"]) == 600


# ---- 2. 运行时：fake provider 确定性 ----


async def test_extract_method_deterministic(client, tmp_path):
    token = await register_and_login(client, email="method@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    project_id, _ = await make_project_with_library(client, headers, name="method-proj")

    async with get_sessionmaker()() as session:
        paper = await add_paper(
            session,
            project_id=uuid.UUID(project_id),
            title="Method Probe Paper",
            abstract="A probe.",
            full_text_path=_write_fulltext(tmp_path),
        )
        await session.commit()
        outcome = await extract_paper(session, paper, schema_id="method")
        assert outcome.status == "extracted"
        await session.commit()
        row = outcome.row
        assert row.schema_id == "method"
        # fake provider：purpose 回显标题前半（断言 prompt 里带对了论文）
        assert "Method Probe" in row.payload["purpose"]
        assert row.payload["mechanism"]
        assert len(row.payload["baseline"]) == 1
        assert len(row.payload["dataset"]) == 1
        assert row.payload["protocol"]
        assert row.stage_meta["stage"] == "extract_method"
        assert row.stage_meta["version"] == 1


# ---- 3. 双轴索引写入与幽灵清理 ----


async def test_refresh_index_writes_both_axes_and_clears_stale(client):
    token = await register_and_login(client, email="methodidx@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    project_id, _ = await make_project_with_library(client, headers, name="methodidx-proj")

    async with get_sessionmaker()() as session:
        paper = await _add_method_paper(
            session,
            project_id,
            title="Dual Axis Paper",
            purpose="reduce hallucination in language models",
            mechanism="retrieval augmentation with external memory",
        )
        paper_id = paper.id
    assert await _method_vectors_of(paper_id) == ["mechanism", "purpose"]

    # 重抽把 mechanism 抽没了 → 该轴向量清掉，不留幽灵
    async with get_sessionmaker()() as session:
        row = await session.scalar(
            select(PaperExtraction).where(PaperExtraction.paper_id == paper_id)
        )
        row.payload = {"purpose": "reduce hallucination in language models"}
        await session.commit()
        paper = await session.get(Paper, paper_id)
        await refresh_paper_method_index(session, paper)
        await session.commit()
    assert await _method_vectors_of(paper_id) == ["purpose"]


# ---- 4. 检索语义：same_purpose / different_mechanism ----


async def test_search_same_purpose_ranks_by_purpose_axis(client):
    token = await register_and_login(client, email="methodsearch@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    project_id, library_id = await make_project_with_library(
        client, headers, name="methodsearch-proj"
    )

    async with get_sessionmaker()() as session:
        target = await _add_method_paper(
            session,
            project_id,
            title="Hallucination Paper",
            purpose="reduce hallucination in large language models",
            mechanism="retrieval augmentation with external memory",
        )
        other = await _add_method_paper(
            session,
            project_id,
            title="Vision Paper",
            purpose="speed up training of vision transformers",
            mechanism="sparse attention kernels",
        )
        items, mode_used = await search_methods(
            session, library_id, "reduce hallucination in language models"
        )
        assert mode_used == "semantic"
        assert [c["paper_id"] for c in items] == [target.id, other.id]
        assert items[0]["similarity"] > items[1]["similarity"]
        # 卡片带全五元组字段
        assert items[0]["purpose"] and items[0]["mechanism"]
        assert "baseline" in items[0] and "dataset" in items[0]


async def test_search_different_mechanism_ranks_far_mechanism_first(client):
    token = await register_and_login(client, email="methodana@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    project_id, library_id = await make_project_with_library(
        client, headers, name="methodana-proj"
    )

    query = "reduce hallucination via retrieval augmentation and external memory"
    async with get_sessionmaker()() as session:
        same_mech = await _add_method_paper(
            session,
            project_id,
            title="Same Mechanism Paper",
            purpose="reduce hallucination in language models",
            mechanism="retrieval augmentation with external memory lookup",
        )
        diff_mech = await _add_method_paper(
            session,
            project_id,
            title="Different Mechanism Paper",
            purpose="reduce hallucination in language models",
            mechanism="contrastive decoding with token pruning",
        )
        # 同目的：same_purpose 下两篇都命中
        items, _ = await search_methods(session, library_id, query, mode="same_purpose")
        assert {c["paper_id"] for c in items} == {same_mech.id, diff_mech.id}
        # 异机制：目的相近的池子里，机制离查询最远的排最前
        items, mode_used = await search_methods(
            session, library_id, query, mode="different_mechanism"
        )
        assert mode_used == "semantic"
        assert [c["paper_id"] for c in items] == [diff_mech.id, same_mech.id]
        assert items[0]["mechanism_similarity"] < items[1]["mechanism_similarity"]

        # 缺机制轴的论文没法排远近：不进 different_mechanism 结果
        no_mech = await _add_method_paper(
            session,
            project_id,
            title="No Mechanism Paper",
            purpose="reduce hallucination in language models",
            mechanism=None,
        )
        items, _ = await search_methods(
            session, library_id, query, mode="different_mechanism"
        )
        assert no_mech.id not in {c["paper_id"] for c in items}


async def test_search_keyword_fallback_when_embeddings_unavailable(client, monkeypatch):
    """嵌入不可用时降级为确定性关键词匹配，mode_used 如实上报。"""
    token = await register_and_login(client, email="methodkw@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    project_id, library_id = await make_project_with_library(
        client, headers, name="methodkw-proj"
    )

    async with get_sessionmaker()() as session:
        hit = await _add_method_paper(
            session,
            project_id,
            title="Keyword Hit Paper",
            purpose="reduce hallucination in language models",
            mechanism="retrieval augmentation",
        )
        miss = await _add_method_paper(
            session,
            project_id,
            title="Keyword Miss Paper",
            purpose="speed up vision transformers",
            mechanism="sparse kernels",
        )

        async def _raise(*args, **kwargs):
            raise NotImplementedError("no embedding")

        import app.services.embedding as embedding_service

        monkeypatch.setattr(embedding_service, "embed_query", _raise)
        items, mode_used = await search_methods(
            session, library_id, "reduce hallucination language models"
        )
        assert mode_used == "keyword"
        assert [c["paper_id"] for c in items] == [hit.id, miss.id]


# ---- 5. 增量钩子：enrich 抽方法卡 + 同步刷索引 ----


async def test_enrich_hook_builds_method_index(client, tmp_path):
    token = await register_and_login(client, email="methodhook@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    project_id, _ = await make_project_with_library(client, headers, name="methodhook-proj")

    async with get_sessionmaker()() as session:
        paper = await add_paper(
            session,
            project_id=uuid.UUID(project_id),
            title="Hooked Method Paper",
            full_text_path=_write_fulltext(tmp_path),
        )
        await session.commit()
        paper_id = paper.id

    async with get_sessionmaker()() as session:
        paper = await session.get(Paper, paper_id)
        await paper_enrich.enrich_paper(
            session, paper, target=None, user_id=None, project_id=None, emit=_noop_emit
        )

    async with get_sessionmaker()() as session:
        rows = (
            (
                await session.execute(
                    select(PaperExtraction).where(PaperExtraction.paper_id == paper_id)
                )
            )
            .scalars()
            .all()
        )
    assert sorted(r.schema_id for r in rows) == ["method", "skeleton"]
    assert await _method_vectors_of(paper_id) == ["mechanism", "purpose"]


async def test_enrich_hook_zero_output_without_fulltext(client):
    """golden 保全：无全文时方法卡零输出、双轴索引零行（bibtex 导入链路不受影响）。"""
    token = await register_and_login(client, email="methodhook2@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    project_id, _ = await make_project_with_library(client, headers, name="methodhook2-proj")

    async with get_sessionmaker()() as session:
        paper = await add_paper(
            session,
            project_id=uuid.UUID(project_id),
            title="Bibtex Method Paper",
            abstract="No pdf, no full text.",
        )
        await session.commit()
        paper_id = paper.id

    async with get_sessionmaker()() as session:
        paper = await session.get(Paper, paper_id)
        await paper_enrich.enrich_paper(
            session, paper, target=None, user_id=None, project_id=None, emit=_noop_emit
        )

    async with get_sessionmaker()() as session:
        rows = (
            (
                await session.execute(
                    select(PaperExtraction).where(PaperExtraction.paper_id == paper_id)
                )
            )
            .scalars()
            .all()
        )
    assert rows == []
    assert await _method_vectors_of(paper_id) == []


# ---- 6. 端点与可见性 ----


async def test_methods_endpoints(client):
    token = await register_and_login(client, email="methodapi@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    project_id, library_id = await make_project_with_library(
        client, headers, name="methodapi-proj"
    )

    async with get_sessionmaker()() as session:
        paper = await _add_method_paper(
            session,
            project_id,
            title="Endpoint Method Paper",
            purpose="reduce hallucination in language models",
            mechanism="retrieval augmentation",
            baseline=["vanilla decoding"],
            dataset=["truthfulqa"],
            protocol="run the benchmark three times",
        )
        paper_id = str(paper.id)

    # 列表：五元组卡
    resp = await client.get(f"/api/libraries/{library_id}/methods", headers=headers)
    assert resp.status_code == 200, resp.text
    cards = resp.json()
    assert len(cards) == 1
    card = cards[0]
    assert card["paper_id"] == paper_id
    assert card["title"] == "Endpoint Method Paper"
    assert card["baseline"] == ["vanilla decoding"]
    assert card["dataset"] == ["truthfulqa"]
    assert card["protocol"] == "run the benchmark three times"
    assert card["similarity"] is None

    # 检索：两种 mode 都通，带相似度
    for mode in ("same_purpose", "different_mechanism"):
        resp = await client.get(
            f"/api/libraries/{library_id}/methods/search",
            params={"q": "reduce hallucination", "mode": mode},
            headers=headers,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["mode"] == mode
        assert body["mode_used"] == "semantic"
        assert [c["paper_id"] for c in body["items"]] == [paper_id]
        assert body["items"][0]["similarity"] is not None

    # 非法 mode 直接 422
    resp = await client.get(
        f"/api/libraries/{library_id}/methods/search",
        params={"q": "x", "mode": "nope"},
        headers=headers,
    )
    assert resp.status_code == 422

    # 未登录不可读
    anon = await client.get(f"/api/libraries/{library_id}/methods")
    assert anon.status_code == 401
    anon = await client.get(
        f"/api/libraries/{library_id}/methods/search", params={"q": "x"}
    )
    assert anon.status_code == 401


async def test_methods_endpoints_hidden_for_foreign_personal_library(client):
    """个人库对外人按不存在处理（404），方法库端点与其余读端点同口径。"""
    owner_token = await register_and_login(client, email="methodowner@example.com")
    owner_headers = {"Authorization": f"Bearer {owner_token}"}
    resp = await client.post(
        "/api/libraries",
        json={"name": "personal-methods", "statement": "private methods library"},
        headers=owner_headers,
    )
    assert resp.status_code == 201, resp.text
    library_id = resp.json()["id"]

    # 创建者本人可读
    resp = await client.get(f"/api/libraries/{library_id}/methods", headers=owner_headers)
    assert resp.status_code == 200

    stranger_token = await register_and_login(client, email="methodstranger@example.com")
    stranger_headers = {"Authorization": f"Bearer {stranger_token}"}
    resp = await client.get(f"/api/libraries/{library_id}/methods", headers=stranger_headers)
    assert resp.status_code == 404
    resp = await client.get(
        f"/api/libraries/{library_id}/methods/search",
        params={"q": "x"},
        headers=stranger_headers,
    )
    assert resp.status_code == 404
