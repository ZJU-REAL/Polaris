"""文献→假设四段管线（#648，services/hypothesis_pipeline.py）：逐段确定性用例。

检索是严格的语义检索（fake 的词袋嵌入：用词重叠越多越靠前），测试库在塞完片段后
建好向量（tests/vector_helpers.index_chunks）；fake provider 的四个 marker 输出全部
确定性（见 core/llm/fake.py），因此每段的输入→输出可以精确断言：

- generate：三路灵感取材（语义近邻 / 引文 2 跳远端 / 多样窗）+ 去重折叠；
- ground：拆子命题 → top-12 检索 → 只许从检索集内选 + 越界引用降 speculation；
- novelty：换措辞复检 + 逐子命题 verdict（没有依据的子命题也判，复检空手的不判）；
- feasibility：确定性资源信号 + fake 风险论证；
- score：novel 比例 × support 覆盖率 的乘积公式。
"""

import json
import uuid

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import select

from app.core.db import get_sessionmaker
from app.core.llm import budgets
from app.core.llm.errors import (
    LLMAuthError,
    LLMError,
    LLMOverloadedError,
    LLMRateLimitedError,
    LLMRefusalError,
    LLMSchemaError,
)
from app.core.llm.fake import FakeProvider
from app.core.llm.router import LLMRouter
from app.models.library_direction import DirectionLibrary
from app.models.paper import Paper, PaperChunk
from app.models.paper_citation import PaperCitation
from app.models.paper_extraction import PaperExtraction
from app.services import hypothesis_pipeline as pipeline
from app.services import library_rag
from app.services.method_index import refresh_paper_method_index
from tests.conftest import add_concept, add_paper, make_project_with_library, register_and_login
from tests.vector_helpers import ensure_space, index_chunks

DIRECTION = "agent planning verification"

# 语料按「哪条查询能命中哪篇论文」精确埋词：A 被方向命中；B 只能靠 A→M→B 的
# 引文 2 跳进灵感；C 不含任何查询词，只能被多样窗捞到
BODY_A = "agent planning verification with tree search. " + "规划方法的细节论述。" * 20
BODY_M = "intermediate hop paper body. " + "中间论文的论述。" * 20
BODY_B = "distant structural relative body. " + "远端论文的论述。" * 20
BODY_C = "unrelated diverse window body. " + "多样窗论文的论述。" * 20


async def _setup(client, *, with_citations: bool = False, indexed: bool = True):
    """建库 + 四篇带片段的成员论文（可选 A→M→B 引文链），返回 (library_id, ids)。

    ``indexed=False``：片段不建向量（平台已有向量空间，但这个库检索不到任何东西）。
    """
    token = await register_and_login(client)
    headers = {"Authorization": f"Bearer {token}"}
    project_id, library_id = await make_project_with_library(
        client, headers, name="pipeline-lib", statement="假设管线测试库"
    )
    bodies = {"A": BODY_A, "M": BODY_M, "B": BODY_B, "C": BODY_C}
    ids: dict[str, uuid.UUID] = {}
    async with get_sessionmaker()() as session:
        for key, body in bodies.items():
            paper = await add_paper(
                session,
                project_id=project_id,
                title=f"Pipeline Paper {key}",
                abstract=f"abstract {key}",
                year=2020 + len(ids),
                venue="VenueX" if key in ("A", "M") else "VenueY",
                relevance_score=0.9,
                status="compiled",
            )
            session.add(PaperChunk(paper_id=paper.id, seq=0, text=body, source="fulltext"))
            ids[key] = paper.id
        if with_citations:
            # A→M→B：B 与 A 图距离 2，是「非直接邻居」的远端灵感来源
            session.add(
                PaperCitation(
                    citing_paper_id=ids["A"],
                    cited_paper_id=ids["M"],
                    ref_index=1,
                    cited_ref_raw="[1] Pipeline Paper M.",
                )
            )
            session.add(
                PaperCitation(
                    citing_paper_id=ids["M"],
                    cited_paper_id=ids["B"],
                    ref_index=1,
                    cited_ref_raw="[1] Pipeline Paper B.",
                )
            )
        await session.commit()
        if indexed:
            await index_chunks(session)
        else:
            await ensure_space(session)
    return library_id, ids


# ---- 纯函数：去重折叠 / 越界引用降级 / 评分 ----


def test_dedup_candidates_collapses_near_duplicates():
    """相似度 >0.85 折叠（保留先出现的）；措辞差异大的都保留；空陈述丢弃。"""
    kept = pipeline.dedup_candidates(
        [
            {"statement": "Planning improves agent reliability in long tasks"},
            # 近重复：只差一个标点
            {"statement": "Planning improves agent reliability in long tasks!"},
            {"statement": "Retrieval grounding reduces hallucinated citations"},
            {"statement": "  "},  # 空陈述
        ]
    )
    assert [c["statement"] for c in kept] == [
        "Planning improves agent reliability in long tasks",
        "Retrieval grounding reduces hallucinated citations",
    ]


def test_sanitize_grounding_downgrades_out_of_set_citations():
    """越界校验：id 不在该子命题自己的检索集内 → 丢弃；没剩合法 id → speculation。"""
    subclaims = ["s0", "s1", "s2"]
    retrieved = [
        [{"paper_id": "p-a", "title": "A", "snippet": "snip-a"}],
        [{"paper_id": "p-b", "title": "B", "snippet": "snip-b"}],
        [],
    ]
    items = [
        # 合法引用 + 一条越界 id（越界的被静默丢弃，合法的保留）
        {"index": 0, "stance": "support", "paper_ids": ["p-a", "p-forged"]},
        # 全部越界（含跨子命题引用 p-a：不在 s1 自己的检索集里也算越界）→ speculation
        {"index": 1, "stance": "refute", "paper_ids": ["p-a", "p-forged"]},
        # 乱写的立场 → speculation
        {"index": 2, "stance": "definitely-true", "paper_ids": []},
    ]
    out = pipeline.sanitize_grounding(subclaims, retrieved, items)
    assert out[0] == {
        "subclaim": "s0",
        "stance": "support",
        "paper_ids": ["p-a"],
        "snippets": ["snip-a"],
    }
    assert out[1]["stance"] == "speculation" and out[1]["paper_ids"] == []
    assert out[2]["stance"] == "speculation"


def test_sanitize_grounding_missing_item_is_speculation():
    """LLM 漏答某条子命题：诚实缺省为 speculation，而不是报错或编造。"""
    retrieved = [[{"paper_id": "p", "title": "", "snippet": "x"}]]
    out = pipeline.sanitize_grounding(["s0"], retrieved, [])
    assert out == [{"subclaim": "s0", "stance": "speculation", "paper_ids": [], "snippets": []}]


def test_score_hypothesis_is_product_of_ratios():
    """score = novel 比例 × support 覆盖率；空接地为 0（没有子命题就没有依据）。"""
    grounding = [
        {"subclaim": "s0", "stance": "support"},
        {"subclaim": "s1", "stance": "speculation"},
    ]
    report = {"subclaims": [{"verdict": "novel"}, {"verdict": "uncertain"}]}
    assert pipeline.score_hypothesis(grounding, report) == 0.25  # (1/2) × (1/2)
    all_support_novel = [{"subclaim": "s", "stance": "support"}] * 2
    full = {"subclaims": [{"verdict": "novel"}, {"verdict": "novel"}]}
    assert pipeline.score_hypothesis(all_support_novel, full) == 1.0
    assert pipeline.score_hypothesis([], {"subclaims": []}) == 0.0
    assert pipeline.novelty_ratio({"subclaims": []}) == 0.0


# ---- 灵感取材（确定性辅助函数） ----


async def test_citation_far_route_reaches_two_hop_papers(client):
    """引文远端：A 的 2 跳邻居是 B（经 M）；直接邻居 M 不入选，B 的片段入选。"""
    library_id, ids = await _setup(client, with_citations=True)
    async with get_sessionmaker()() as session:
        chunks = await pipeline._citation_far_chunks(
            session, library_id=library_id, seed_paper_ids=[ids["A"]]
        )
    assert [c.paper_id for c in chunks] == [ids["B"]]


async def test_diverse_window_route_excludes_covered_papers(client):
    """多样窗：已被其他两路覆盖的论文让位，等距取样确定性可复现。"""
    library_id, ids = await _setup(client)
    async with get_sessionmaker()() as session:
        first = await pipeline._diverse_window_chunks(
            session, library_id=library_id, exclude_paper_ids={ids["A"], ids["M"]}
        )
        second = await pipeline._diverse_window_chunks(
            session, library_id=library_id, exclude_paper_ids={ids["A"], ids["M"]}
        )
    assert {c.paper_id for c in first} <= {ids["B"], ids["C"]}
    assert [(c.paper_id, c.seq) for c in first] == [(c.paper_id, c.seq) for c in second]


async def test_generate_returns_deduped_candidates_with_usage(client):
    """generate 端到端（fake）：两个回显方向的候选，去重不误伤，usage 有账。"""
    library_id, _ids = await _setup(client, with_citations=True)
    async with get_sessionmaker()() as session:
        result = await pipeline.generate(
            session, LLMRouter(), library_id=library_id, direction=DIRECTION, n_candidates=3
        )
    statements = [c["statement"] for c in result["candidates"]]
    assert len(statements) == 2  # fake 固定两候选，措辞差异大、不被折叠
    assert all(DIRECTION[:20] in s for s in statements)
    assert result["usage"]["prompt_tokens"] > 0
    # 无燃料（本库没有方法卡/概念/缺口产物）：燃料留痕三键恒在且全空，
    # 且没有燃料回显候选——行为与 #670 之前的纯检索路一致
    assert result["trace"]["fuels"] == {"methods": [], "concept_pairs": [], "gaps": []}
    assert not any("燃料组合假设" in s for s in statements)


# ---- 燃料喂养（#670，P2.5 F6）：燃料存在与否改变候选 + 静默降级 ----


async def _seed_fuels(session, library_id, ids):
    """给库埋齐三路燃料：A/M 的方法卡（purpose 含方向词、机制互异）、
    alpha–bridge–gamma 的未连接概念对、B 的缺口条目。"""
    project_id = (await session.get(DirectionLibrary, library_id)).project_id
    # 燃料 1：方法卡 + 双轴索引（fake 词袋嵌入：purpose 用词与方向重叠即相近）
    session.add(
        PaperExtraction(
            paper_id=ids["A"],
            schema_id="method",
            payload={
                "purpose": "agent planning verification purpose",
                "mechanism": "tree search rollout mechanism",
            },
        )
    )
    session.add(
        PaperExtraction(
            paper_id=ids["M"],
            schema_id="method",
            payload={
                "purpose": "agent planning verification purpose",
                "mechanism": "reinforcement fine-tuning mechanism",
            },
        )
    )
    await session.commit()
    for key in ("A", "M"):
        await refresh_paper_method_index(session, await session.get(Paper, ids[key]))
    # 燃料 2：alpha–bridge、bridge–gamma 共现，alpha–gamma 从未同篇 → 未连接对
    alpha = await add_concept(session, project_id=project_id, name="alpha", slug="hyp-alpha")
    bridge = await add_concept(session, project_id=project_id, name="bridge", slug="hyp-bridge")
    gamma = await add_concept(session, project_id=project_id, name="gamma", slug="hyp-gamma")
    await add_paper(
        session,
        project_id=project_id,
        title="Fuel Pair One",
        status="included",
        concepts=[alpha, bridge],
    )
    await add_paper(
        session,
        project_id=project_id,
        title="Fuel Pair Two",
        status="included",
        concepts=[bridge, gamma],
    )
    # 燃料 3：B 的缺口台账条目
    session.add(
        PaperExtraction(
            paper_id=ids["B"],
            schema_id="gaps",
            payload={
                "entries": [
                    {
                        "kind": "gap",
                        "statement": "跨域泛化仍未解决（fuel gap）",
                        "source_span": "cross-domain generalization remains open",
                    }
                ]
            },
        )
    )
    await session.commit()


async def test_generate_fuels_change_candidates_and_trace(client):
    """P2.5 F6 出口判据：同一方向，燃料存在 → fake 多产一条回显「燃料节 +
    条目数」的候选（与无燃料时可断言地不同），trace.fuels 记齐消费清单。"""
    library_id, ids = await _setup(client)
    async with get_sessionmaker()() as session:
        await _seed_fuels(session, library_id, ids)
        result = await pipeline.generate(
            session, LLMRouter(), library_id=library_id, direction=DIRECTION, n_candidates=3
        )
    statements = [c["statement"] for c in result["candidates"]]
    assert len(statements) == 3  # 纯检索路只有 2 条：燃料确实改变了候选
    fuel_echo = statements[2]
    # fake 回显收到的燃料节（按节名排序）与条目数：三路都被喂进了 prompt
    assert "method_analogies=2 open_gaps=1 unconnected_pairs=1" in fuel_echo
    fuels = result["trace"]["fuels"]
    assert set(fuels["methods"]) == {str(ids["A"]), str(ids["M"])}
    assert fuels["gaps"] == [str(ids["B"])]
    # 概念对的 a/c 取 uuid 规范序（随机 uuid 下方向不定），按名字集合断言
    assert [sorted((p["concept_a"], p["concept_c"])) for p in fuels["concept_pairs"]] == [
        ["alpha", "gamma"]
    ]


async def test_generate_fuel_service_failure_degrades_silently(client, monkeypatch):
    """三路燃料服务全炸也不许拖垮 generate：静默降级为空、行为等同纯检索路。"""
    library_id, ids = await _setup(client)
    async with get_sessionmaker()() as session:
        await _seed_fuels(session, library_id, ids)  # 数据在，但服务炸了

        async def _boom(*args, **kwargs):
            raise RuntimeError("fuel service down")

        monkeypatch.setattr(pipeline.method_index, "search_methods", _boom)
        monkeypatch.setattr(pipeline.concept_fuels, "mine_unconnected_pairs", _boom)
        monkeypatch.setattr(pipeline.gap_ledger, "library_gaps", _boom)
        result = await pipeline.generate(
            session, LLMRouter(), library_id=library_id, direction=DIRECTION, n_candidates=3
        )
    statements = [c["statement"] for c in result["candidates"]]
    assert len(statements) == 2  # 与纯检索路相同的两条 fake 候选
    assert not any("燃料组合假设" in s for s in statements)
    assert result["trace"]["fuels"] == {"methods": [], "concept_pairs": [], "gaps": []}


# ---- 接地 / 查新 / 可行性（fake 端到端） ----


async def test_ground_selects_real_paper_and_marks_speculation(client):
    """ground：拆 2 条子命题；第一条 support 引真实库内论文并回填片段，
    第二条 speculation（fake 确定性行为，也是无证据时的诚实缺省）。"""
    library_id, ids = await _setup(client)
    async with get_sessionmaker()() as session:
        result = await pipeline.ground(
            session,
            LLMRouter(),
            library_id=library_id,
            statement=f"关于 {DIRECTION} 的机制假设",
        )
    grounding = result["grounding"]
    assert [g["stance"] for g in grounding] == ["support", "speculation"]
    assert grounding[0]["paper_ids"] == [str(ids["A"])]  # 方向词只埋在 A 里
    assert grounding[0]["snippets"] and "agent planning" in grounding[0]["snippets"][0]
    assert result["usage"]["prompt_tokens"] > 0


async def test_ground_without_hits_is_all_speculation(client):
    """库里检索不到任何片段：不许硬选 → 全部 speculation。"""
    library_id, _ids = await _setup(client, indexed=False)
    async with get_sessionmaker()() as session:
        result = await pipeline.ground(
            session,
            LLMRouter(),
            library_id=library_id,
            statement="冰川沉积物同位素定年",  # 与库内语料零重叠
        )
    assert [g["stance"] for g in result["grounding"]] == ["speculation", "speculation"]
    assert all(g["paper_ids"] == [] for g in result["grounding"])


async def test_novelty_judges_every_subclaim_including_unsupported_ones(client):
    """novelty：每条子命题都送 judge（fake：第一条 novel 并引证据首 id，其余
    uncertain）。接地判成 speculation 的也要判——假设里新的那部分正是它们。"""
    library_id, ids = await _setup(client)
    grounding = [
        {
            "subclaim": f"{DIRECTION} 的机制子命题",
            "stance": "support",
            "paper_ids": [str(ids["A"])],
        },
        {"subclaim": "无证据的推测子命题", "stance": "speculation", "paper_ids": []},
    ]
    async with get_sessionmaker()() as session:
        result = await pipeline.novelty(
            session,
            LLMRouter(),
            library_id=library_id,
            statement="stmt",
            grounding=grounding,
        )
    report = result["report"]["subclaims"]
    assert [r["verdict"] for r in report] == ["novel", "uncertain"]
    assert [r["judged"] for r in report] == [True, True]
    assert report[0]["paper_ids"] == [str(ids["A"])]  # 复检查询里 A 排第一
    assert result["usage"]["prompt_tokens"] > 0
    # 每条子命题一条复检留痕，用的是换过措辞的查询
    queries = result["trace"]["queries"]
    assert len(queries) == 2 and all(q["query"].startswith("已有研究") for q in queries)


async def test_novelty_of_an_unsupported_hypothesis_is_still_judged(client):
    """全 speculation 的假设：库里有可对照的文献，就要判它是不是新的。
    只判有依据的子命题时，新颖比例永远接近零、子假设全部被剪。"""
    library_id, _ids = await _setup(client)
    grounding = [{"subclaim": "s", "stance": "speculation", "paper_ids": []}]
    async with get_sessionmaker()() as session:
        result = await pipeline.novelty(
            session, LLMRouter(), library_id=library_id, statement="stmt", grounding=grounding
        )
    entry = result["report"]["subclaims"][0]
    assert entry["verdict"] == "novel" and entry["judged"] is True
    assert result["usage"]["prompt_tokens"] > 0
    assert pipeline.novelty_ratio(result["report"]) == 1.0


async def test_novelty_without_any_retrieved_evidence_skips_the_model(client):
    """复检什么都没捞到：没有文献可对照，不调模型（usage 为零）、如实 uncertain。"""
    library_id, _ids = await _setup(client, indexed=False)
    grounding = [{"subclaim": "s", "stance": "speculation", "paper_ids": []}]
    async with get_sessionmaker()() as session:
        result = await pipeline.novelty(
            session, LLMRouter(), library_id=library_id, statement="stmt", grounding=grounding
        )
    entry = result["report"]["subclaims"][0]
    assert entry["verdict"] == "uncertain" and entry["judged"] is False
    assert result["usage"] == {"prompt_tokens": 0, "completion_tokens": 0}
    assert len(result["trace"]["queries"]) == 1  # 查过，只是空手


async def test_feasibility_signals_are_deterministic(client):
    """feasibility：venue/年份/密度信号由代码统计（fake 不碰），风险论证来自 LLM。"""
    library_id, ids = await _setup(client)
    grounding = [
        {
            "subclaim": "s0",
            "stance": "support",
            "paper_ids": [str(ids["A"]), str(ids["B"])],
        },
        {"subclaim": "s1", "stance": "speculation", "paper_ids": []},
    ]
    async with get_sessionmaker()() as session:
        result = await pipeline.feasibility(
            session,
            LLMRouter(),
            library_id=library_id,
            statement=DIRECTION,
            grounding=grounding,
        )
    signals = result["feasibility"]["signals"]
    assert signals["grounded_paper_count"] == 2
    assert signals["venues"] == {"VenueX": 1, "VenueY": 1}
    assert signals["year_range"] == [2020, 2022]  # A=2020、B=2022（M 不在接地集里）
    assert signals["related_chunk_hits"] >= 1  # 方向词埋在 A 的片段里
    assert "fake 可行性" in result["feasibility"]["risk_note"]


async def test_feasibility_empty_grounding_still_reports_signals(client):
    """全推测的假设也要有可行性信号（都是 0/空）：信号是统计不是判断。"""
    library_id, _ids = await _setup(client)
    async with get_sessionmaker()() as session:
        result = await pipeline.feasibility(
            session,
            LLMRouter(),
            library_id=library_id,
            statement="冰川沉积物同位素定年",
            grounding=[{"subclaim": "s", "stance": "speculation", "paper_ids": []}],
        )
    signals = result["feasibility"]["signals"]
    assert signals["grounded_paper_count"] == 0
    assert signals["venues"] == {} and signals["year_range"] is None
    assert signals["related_chunk_hits"] == 0


# ---- 严格检索：语义检索用不了时明确失败，不退回关键词 ----


async def _setup_without_any_vectors(client):
    """有片段、但平台从没建过向量（没有向量空间）的库。"""
    token = await register_and_login(client)
    headers = {"Authorization": f"Bearer {token}"}
    project_id, library_id = await make_project_with_library(
        client, headers, name="unindexed-lib", statement="没有语义索引的库"
    )
    async with get_sessionmaker()() as session:
        paper = await add_paper(
            session, project_id=project_id, title="Unindexed", status="compiled"
        )
        session.add(PaperChunk(paper_id=paper.id, seq=0, text=BODY_A, source="fulltext"))
        await session.commit()
    return library_id


async def test_retrieval_without_an_index_raises_instead_of_keyword_search(client):
    library_id = await _setup_without_any_vectors(client)
    async with get_sessionmaker()() as session:
        # 问答用的宽松检索照旧降级到关键词，能命中
        loose = await library_rag.retrieve_round(
            session, library_id=library_id, query=DIRECTION, user_id=None, project_id=None
        )
        assert len(loose) == 1
        # 假设管线的严格检索：没有语义索引就是明确的、要人处理的错误
        with pytest.raises(library_rag.RetrievalUnavailableError) as exc:
            await pipeline.generate(
                session, LLMRouter(), library_id=library_id, direction=DIRECTION
            )
    err = exc.value
    assert isinstance(err, LLMError)
    assert err.code == "RETRIEVAL_UNAVAILABLE" and err.transient is False
    assert "语义索引" in err.user_message


@pytest.mark.parametrize(
    ("failure", "transient"),
    [
        (httpx.ConnectError("gateway down"), True),
        (
            httpx.HTTPStatusError(
                "rate limited",
                request=httpx.Request("POST", "http://gw.test/v1/embeddings"),
                response=httpx.Response(429),
            ),
            True,
        ),
        (
            httpx.HTTPStatusError(
                "bad key",
                request=httpx.Request("POST", "http://gw.test/v1/embeddings"),
                response=httpx.Response(401),
            ),
            False,
        ),
    ],
)
async def test_embedding_failure_is_typed_by_whether_a_retry_can_help(
    client, monkeypatch, failure, transient
):
    library_id, _ids = await _setup(client)

    async def _fail(*_args, **_kwargs):
        raise failure

    monkeypatch.setattr(library_rag, "embed_query", _fail)
    async with get_sessionmaker()() as session:
        with pytest.raises(library_rag.RetrievalUnavailableError) as exc:
            await pipeline.ground(session, LLMRouter(), library_id=library_id, statement="s")
        assert exc.value.transient is transient
        # 宽松检索不受影响：同样的失败下照旧降级
        rows = await library_rag.retrieve_round(
            session, library_id=library_id, query=DIRECTION, user_id=None, project_id=None
        )
        assert rows


# ---- 模型调用失败：判断不出来才降级，没跑成要上抛 ----


def _router_with(fake: FakeProvider) -> LLMRouter:
    router = LLMRouter()
    router.override_provider(fake)
    return router


def _support_grounding(ids) -> list[dict]:
    return [{"subclaim": DIRECTION, "stance": "support", "paper_ids": [str(ids["A"])]}]


async def test_novelty_degrades_to_uncertain_only_when_the_model_cannot_judge(client):
    library_id, ids = await _setup(client)
    fake = FakeProvider()
    fake.fail_next(LLMSchemaError("still invalid after the repair"), schema=pipeline.NoveltyOut)
    async with get_sessionmaker()() as session:
        result = await pipeline.novelty(
            session,
            _router_with(fake),
            library_id=library_id,
            statement="stmt",
            grounding=_support_grounding(ids),
        )
        entry = result["report"]["subclaims"][0]
        assert entry["verdict"] == "uncertain" and entry["judged"] is False
        # 限流不是「判断不出来」：这一步没跑成，原样抛出去让上层重试
        fake.fail_next(LLMRateLimitedError("HTTP 429"), schema=pipeline.NoveltyOut)
        with pytest.raises(LLMRateLimitedError):
            await pipeline.novelty(
                session,
                _router_with(fake),
                library_id=library_id,
                statement="stmt",
                grounding=_support_grounding(ids),
            )


async def test_feasibility_keeps_signals_when_the_risk_note_cannot_be_written(client):
    library_id, ids = await _setup(client)
    fake = FakeProvider()
    fake.fail_next(LLMRefusalError("cyber"), schema=pipeline.RiskNoteOut)
    async with get_sessionmaker()() as session:
        result = await pipeline.feasibility(
            session,
            _router_with(fake),
            library_id=library_id,
            statement=DIRECTION,
            grounding=_support_grounding(ids),
        )
        assert result["feasibility"]["signals"]["grounded_paper_count"] == 1
        assert "风险论证生成失败" in result["feasibility"]["risk_note"]
        fake.fail_next(LLMAuthError("HTTP 401"), schema=pipeline.RiskNoteOut)
        with pytest.raises(LLMAuthError):
            await pipeline.feasibility(
                session,
                _router_with(fake),
                library_id=library_id,
                statement=DIRECTION,
                grounding=_support_grounding(ids),
            )


async def test_generate_and_ground_failures_are_typed_not_invalid_json(client):
    """生成与接地没有降级路径：模型调用失败就是这一步失败，错误带着是否可重试。"""
    library_id, _ids = await _setup(client)
    fake = FakeProvider()
    async with get_sessionmaker()() as session:
        fake.fail_next(LLMOverloadedError("HTTP 529"), schema=pipeline.GenerateOut)
        with pytest.raises(LLMOverloadedError) as exc:
            await pipeline.generate(
                session, _router_with(fake), library_id=library_id, direction=DIRECTION
            )
        assert exc.value.transient is True
        fake.fail_next(LLMSchemaError("bad shape"), schema=pipeline.GroundOut)
        with pytest.raises(LLMSchemaError) as exc:
            await pipeline.ground(session, _router_with(fake), library_id=library_id, statement="s")
        assert exc.value.transient is False


# ---- schema 与上限 ----


def test_output_schemas_reject_empty_statements_and_unknown_labels():
    with pytest.raises(ValidationError):
        pipeline.GenerateOut.model_validate({"candidates": []})
    with pytest.raises(ValidationError):
        pipeline.GenerateOut.model_validate({"candidates": [{"statement": "  ", "rationale": ""}]})
    with pytest.raises(ValidationError):
        pipeline.SubclaimsOut.model_validate({"subclaims": []})
    with pytest.raises(ValidationError):
        pipeline.GroundOut.model_validate(
            {"items": [{"index": 0, "stance": "maybe", "paper_ids": []}]}
        )
    with pytest.raises(ValidationError):
        pipeline.NoveltyOut.model_validate(
            {"items": [{"index": 0, "verdict": "new", "paper_ids": []}]}
        )
    out = pipeline.GenerateOut.model_validate(
        {"candidates": [{"statement": "  trimmed  ", "rationale": "r"}]}
    )
    assert out.candidates[0].statement == "trimmed"


def test_every_schema_is_accepted_by_structured_outputs():
    """发给 Claude 的 schema：对象全封闭、不带接口不支持的约束。"""
    import anthropic

    from app.agents.voyage.actions_discovery import SeedOut
    from app.services.hypothesis_tournament import CompareOut

    for schema in (
        SeedOut,
        pipeline.GenerateOut,
        pipeline.SubclaimsOut,
        pipeline.GroundOut,
        pipeline.NoveltyOut,
        pipeline.RiskNoteOut,
        CompareOut,
    ):
        wire = json.dumps(anthropic.transform_schema(schema))
        assert '"additionalProperties": false' in wire, schema.__name__
        assert '"minLength"' not in wire and '"minimum"' not in wire, schema.__name__
        assert "$ref" not in wire or "$defs" in wire


def test_discovery_limits_live_in_budgets():
    assert (budgets.HYPOTHESIS_EVIDENCE_PER_SUBCLAIM, pipeline.GROUND_TOP_K) == (12, 12)
    assert (budgets.HYPOTHESIS_SNIPPET_CHARS, pipeline.SNIPPET_CHARS) == (1_500, 1_500)
    assert (budgets.HYPOTHESIS_MAX_SUBCLAIMS, pipeline.MAX_SUBCLAIMS) == (6, 6)
    assert pipeline.INSPIRATION_PER_ROUTE == pipeline.FUEL_PER_ROUTE == 8
    assert (library_rag.TOP_K, library_rag.RERANK_POOL) == (24, 80)
    assert budgets.DISCOVERY_CHILDREN_PER_ROUND == 4
    assert budgets.DISCOVERY_DEFAULT_EXPANSIONS == 4
    assert str(pipeline.MAX_SUBCLAIMS) in pipeline.GROUND_SPLIT_SYSTEM_PROMPT


async def test_evidence_snippets_carry_up_to_1500_chars(client):
    library_id, ids = await _setup(client)
    async with get_sessionmaker()() as session:
        chunk = (
            await session.execute(select(PaperChunk).where(PaperChunk.paper_id == ids["A"]))
        ).scalar_one()
        chunk.text = "agent planning verification " + "x" * 3_000
        await session.commit()
        await index_chunks(session, paper_ids=[ids["A"]])
        result = await pipeline.ground(
            session, LLMRouter(), library_id=library_id, statement=f"关于 {DIRECTION} 的机制假设"
        )
    snippet = result["grounding"][0]["snippets"][0]
    assert len(snippet) == 1_500
