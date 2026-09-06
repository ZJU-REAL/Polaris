"""文献→假设四段管线（#648，services/hypothesis_pipeline.py）：逐段确定性用例。

sqlite 测试库没有 pgvector，检索原语走关键词降级路径；fake provider 的四个
marker 输出全部确定性（见 core/llm/fake.py），因此每段的输入→输出可以精确断言：

- generate：三路灵感取材（语义近邻 / 引文 2 跳远端 / 多样窗）+ 去重折叠；
- ground：拆子命题 → top-5 检索 → 只许从检索集内选 + 越界引用降 speculation；
- novelty：换措辞复检 + 逐子命题 verdict（speculation 不送 judge）；
- feasibility：确定性资源信号 + fake 风险论证；
- score：novel 比例 × support 覆盖率 的乘积公式。
"""

import uuid

from app.core.db import get_sessionmaker
from app.core.llm.router import LLMRouter
from app.models.paper import PaperChunk
from app.models.paper_citation import PaperCitation
from app.services import hypothesis_pipeline as pipeline
from tests.conftest import add_paper, make_project_with_library, register_and_login

DIRECTION = "agent planning verification"

# 语料按「哪条查询能命中哪篇论文」精确埋词：A 被方向命中；B 只能靠 A→M→B 的
# 引文 2 跳进灵感；C 不含任何查询词，只能被多样窗捞到
BODY_A = "agent planning verification with tree search. " + "规划方法的细节论述。" * 20
BODY_M = "intermediate hop paper body. " + "中间论文的论述。" * 20
BODY_B = "distant structural relative body. " + "远端论文的论述。" * 20
BODY_C = "unrelated diverse window body. " + "多样窗论文的论述。" * 20


async def _setup(client, *, with_citations: bool = False):
    """建库 + 四篇带片段的成员论文（可选 A→M→B 引文链），返回 (library_id, ids)。"""
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
    """库里检索不到任何相关片段：不许硬选 → 全部 speculation。"""
    library_id, _ids = await _setup(client)
    async with get_sessionmaker()() as session:
        result = await pipeline.ground(
            session,
            LLMRouter(),
            library_id=library_id,
            statement="冰川沉积物同位素定年",  # 与库内语料零重叠
        )
    assert [g["stance"] for g in result["grounding"]] == ["speculation", "speculation"]
    assert all(g["paper_ids"] == [] for g in result["grounding"])


async def test_novelty_judges_only_grounded_subclaims(client):
    """novelty：support 子命题送 judge（fake 判 novel、引证据首 id）；
    speculation 不送 judge、如实 uncertain / judged=False。"""
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
    assert [r["judged"] for r in report] == [True, False]
    assert report[0]["paper_ids"] == [str(ids["A"])]  # 复检查询仍只命中 A
    assert result["usage"]["prompt_tokens"] > 0


async def test_novelty_all_speculation_skips_llm(client):
    """全 speculation：没有可判的子命题，不调 LLM（usage 为零）、全 uncertain。"""
    library_id, _ids = await _setup(client)
    grounding = [{"subclaim": "s", "stance": "speculation", "paper_ids": []}]
    async with get_sessionmaker()() as session:
        result = await pipeline.novelty(
            session, LLMRouter(), library_id=library_id, statement="stmt", grounding=grounding
        )
    assert result["report"]["subclaims"][0]["verdict"] == "uncertain"
    assert result["usage"] == {"prompt_tokens": 0, "completion_tokens": 0}


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
