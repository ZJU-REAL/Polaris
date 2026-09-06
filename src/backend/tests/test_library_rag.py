"""库级 agentic RAG（#644）：四件套逐环节确定性用例 + 端到端问答。

sqlite 测试库没有 pgvector，检索走关键词降级路径——四件套的判定全都不依赖
向量相似度，语料按「哪条查询能命中哪篇论文」精确埋词：

- 问题 "planning agent approaches" 只命中论文 A；
- fake provider 的扩展查询固定为 "expansion probe query (fake)"，只命中论文 B
  （正文埋 "expansion probe"）；
- 论文 C 不含任何查询词，只能靠 A→C 的引文边补召回进来。
"""

import uuid
from pathlib import Path

from sqlalchemy import select

from app.core.db import get_sessionmaker
from app.models.paper import PaperChunk
from app.models.paper_citation import PaperCitation
from app.services import library_rag
from tests.conftest import add_paper, make_project_with_library, register_and_login

QUESTION = "planning agent approaches"
FAKE_EXPANSION_QUERY = "expansion probe query (fake)"

# 每篇正文只含「自己该被谁命中」的词，互不串词（fake/query 等词也都只出现在 B）
BODY_A = "planning agent approaches with tree search. " + "规划方法的细节论述。" * 60
BODY_B = "expansion probe details for the follow-up. " + "补充角度的细节论述。" * 60
BODY_C = "structural neighbor without matching terms. " + "结构性相关的邻居论述。" * 60


async def _setup(client, *, with_citation_edge=False, papers=("A", "B", "C")):
    """建库 + 三篇论文（可选 A→C 引文边）+ 全文索引，返回 (headers, library_id, ids)。"""
    token = await register_and_login(client)
    headers = {"Authorization": f"Bearer {token}"}
    project_id, library_id = await make_project_with_library(
        client, headers, name="rag-lib", statement="LLM agent 规划"
    )

    import tempfile

    txt_dir = Path(tempfile.mkdtemp(prefix="polaris-rag-"))
    bodies = {"A": BODY_A, "B": BODY_B, "C": BODY_C}
    titles = {"A": "Planning Agents Survey", "B": "Probe Target", "C": "Cited Neighbor"}
    ids: dict[str, uuid.UUID] = {}
    async with get_sessionmaker()() as session:
        for key in papers:
            txt = txt_dir / f"{key}.txt"
            txt.write_text(bodies[key], encoding="utf-8")
            paper = await add_paper(
                session,
                project_id=project_id,
                title=titles[key],
                abstract=f"{titles[key]} abstract",
                year=2026,
                relevance_score=0.9,
                status="compiled",
                full_text_path=str(txt),
            )
            ids[key] = paper.id
        if with_citation_edge:
            # A 引用 C：citation 补召回唯一能把 C 拉进证据集的通道
            session.add(
                PaperCitation(
                    citing_paper_id=ids["A"],
                    cited_paper_id=ids["C"],
                    ref_index=1,
                    cited_ref_raw="[1] Cited Neighbor.",
                )
            )
        await session.commit()

    resp = await client.post(f"/api/libraries/{library_id}/index/rebuild", headers=headers)
    assert resp.status_code == 200, resp.text
    return headers, library_id, ids


# ---- 小库直通 ----


async def test_small_library_takes_the_direct_path(client):
    """片段总量在预算内：跳过检索（queries 为空），全部片段作为证据直供作答。"""
    headers, library_id, ids = await _setup(client, papers=("A", "B"))

    resp = await client.post(
        f"/api/libraries/{library_id}/qa", json={"question": QUESTION}, headers=headers
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["queries"] == []
    assert len(body["evidence"]) > 0
    assert all(e["via"] == "direct" for e in body["evidence"])
    # 直通证据覆盖库内全部片段
    async with get_sessionmaker()() as session:
        total = len((await session.execute(select(PaperChunk))).scalars().all())
    assert len(body["evidence"]) == total
    # fake 作答回显问题并引用首条证据；该引用在证据集内，不会被剥离
    assert "fake RAG 作答" in body["answer"] and QUESTION in body["answer"]
    assert f"[{body['evidence'][0]['paper_id']}]" in body["answer"]


async def test_empty_library_answers_without_llm(client):
    """没有任何片段：不作答不编造，明说没内容。"""
    token = await register_and_login(client, email="rag-empty@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    _project_id, library_id = await make_project_with_library(client, headers, name="empty-lib")
    resp = await client.post(
        f"/api/libraries/{library_id}/qa", json={"question": "有什么内容？"}, headers=headers
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["evidence"] == [] and body["queries"] == []
    assert "还没有可检索的内容" in body["answer"]


# ---- 迭代查询扩展 ----


async def test_expansion_round_pulls_in_the_probe_paper(client, monkeypatch):
    """max_rounds=2：首轮命中 A，扩展查询命中 B（via=expansion），queries 记录两条。"""
    monkeypatch.setattr(library_rag, "DIRECT_TOKEN_BUDGET", 0)  # 强制走检索流水线
    headers, library_id, ids = await _setup(client)

    async with get_sessionmaker()() as session:
        result = await library_rag.answer(
            session, library_id, QUESTION, user_id=None, max_rounds=2
        )
    assert result["queries"] == [QUESTION, FAKE_EXPANSION_QUERY]
    via_by_paper = {e["paper_id"]: e["via"] for e in result["evidence"]}
    assert via_by_paper.get(str(ids["A"])) == "vector"
    assert via_by_paper.get(str(ids["B"])) == "expansion"


async def test_max_rounds_one_skips_expansion(client, monkeypatch):
    """max_rounds=1：只跑首轮，不调用扩展环节，B 进不了证据集。"""
    monkeypatch.setattr(library_rag, "DIRECT_TOKEN_BUDGET", 0)
    headers, library_id, ids = await _setup(client)

    async with get_sessionmaker()() as session:
        result = await library_rag.answer(
            session, library_id, QUESTION, user_id=None, max_rounds=1
        )
    assert result["queries"] == [QUESTION]
    assert str(ids["B"]) not in {e["paper_id"] for e in result["evidence"]}


# ---- 引文图补召回 ----


async def test_citation_edge_recalls_the_neighbor(client, monkeypatch):
    """C 不含任何查询词，只有 A→C 的引文边能把它拉进证据集（via=citation）。"""
    monkeypatch.setattr(library_rag, "DIRECT_TOKEN_BUDGET", 0)
    headers, library_id, ids = await _setup(client, with_citation_edge=True)

    async with get_sessionmaker()() as session:
        result = await library_rag.answer(
            session, library_id, QUESTION, user_id=None, max_rounds=2
        )
    via_by_paper = {e["paper_id"]: e["via"] for e in result["evidence"]}
    assert via_by_paper.get(str(ids["C"])) == "citation"


async def test_without_citation_edge_the_neighbor_stays_out(client, monkeypatch):
    """对照：没有引文边时 C 无法进入证据集（证明上一条确实是引文边的功劳）。"""
    monkeypatch.setattr(library_rag, "DIRECT_TOKEN_BUDGET", 0)
    headers, library_id, ids = await _setup(client, with_citation_edge=False)

    async with get_sessionmaker()() as session:
        result = await library_rag.answer(
            session, library_id, QUESTION, user_id=None, max_rounds=2
        )
    assert str(ids["C"]) not in {e["paper_id"] for e in result["evidence"]}


# ---- 引用越界剥离（纯函数） ----


def test_invalid_citations_are_stripped_and_annotated():
    good = "123e4567-e89b-12d3-a456-426614174000"
    bad = "ffffffff-ffff-4fff-8fff-ffffffffffff"
    text = f"结论一 [{good}]。结论二 [{bad}]。普通编号 [1] 不动。"
    cleaned, stripped = library_rag._strip_invalid_citations(text, {good})
    assert stripped == 1
    assert f"[{good}]" in cleaned and f"[{bad}]" not in cleaned
    assert "[1]" in cleaned  # 非 uuid 形态的方括号不归引用校验管
    assert "已剥离 1 处" in cleaned
    # 全部合法时原样返回、不加注
    same, none_stripped = library_rag._strip_invalid_citations(f"结论 [{good}]。", {good})
    assert none_stripped == 0 and "已剥离" not in same


# ---- 端到端（API，四件套全开） ----


async def test_qa_api_end_to_end_over_the_full_pipeline(client, monkeypatch):
    """扩展 + 引文补召回 + 重排 + 作答全链：证据三种 via 齐备，引用可回溯。"""
    monkeypatch.setattr(library_rag, "DIRECT_TOKEN_BUDGET", 0)
    headers, library_id, ids = await _setup(client, with_citation_edge=True)

    resp = await client.post(
        f"/api/libraries/{library_id}/qa",
        json={"question": QUESTION, "max_rounds": 2},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["queries"] == [QUESTION, FAKE_EXPANSION_QUERY]
    vias = {e["via"] for e in body["evidence"]}
    assert {"vector", "expansion", "citation"} <= vias
    # 每条证据都能回溯：paper_id/chunk_id/snippet 齐备
    for e in body["evidence"]:
        assert e["paper_id"] and e["chunk_id"] and e["snippet"]
    # fake 作答引用首条证据的 paper_id；它在证据集内，未被剥离
    assert f"[{body['evidence'][0]['paper_id']}]" in body["answer"]
    # 重排（fake 恒序）后首条证据来自首轮命中的 A
    assert body["evidence"][0]["paper_id"] == str(ids["A"])

    # 库不存在 → 404（可见性判据与其他库端点同一入口）
    missing = uuid.uuid4()
    resp = await client.post(
        f"/api/libraries/{missing}/qa", json={"question": "hi"}, headers=headers
    )
    assert resp.status_code == 404
