"""M5-C 论文评审测试（docs/api-m5-c.md，fake LLM + 假 tectonic 直接驱动 VoyageEngine）。

- 发起端点：COMPILE_REQUIRED / 同稿件互斥 / kind=paper_review；
- 全链路（通过）：固定六步 → payload 契约 shape、4 条消息（3 评审员 + 主席 Meta）、
  聚合 7.0（低 confidence 降权）、review_passed=true；
- 不通过：REVIEW_FAIL_TEST 全员低分 → revision_notes 写 fact_pack、
  under_review 回 compiled + WS；
- fabricated 强制不通过 + 支撑性 UNSUPPORTED_TEST 标记；
- guardrail 拒绝 → 重生成 ×2 → unreliable 排除聚合；
- 核验三态（respx mock S2/OpenAlex）与 OpenAlex 降级；
- 查错数字比对 / reviewer JSON 校验 / 聚合降权 纯函数单测；
- submit 前置 REVIEW_REQUIRED 与 gate approve override 跳过；
- 评审历史列表与 manuscript session 消息权限。
"""

import uuid
from pathlib import Path

import httpx
import pytest_asyncio
import respx
from sqlalchemy import select

from app.agents.voyage import VoyageEngine
from app.core.db import get_sessionmaker
from app.core.llm.router import LLMRouter
from app.models.manuscript import Manuscript, ManuscriptFile
from app.models.review import ReviewMessage, ReviewSession
from app.services import latex_compile
from app.services import paper_review as pr
from app.services.latex_compile import TectonicRun
from app.services.literature.openalex import OpenAlexClient
from app.services.literature.semantic_scholar import SemanticScholarClient
from tests.conftest import RecordingBus, register_and_login
from tests.test_manuscripts import (
    _create_manuscript,
    _seed_experiment,
    _seed_idea,
    _seed_paper,
    _setup_project,
)

FACT_PACK = {
    "citations": [{"bibkey": "smith2017attention", "title": "Attention", "year": 2017}],
    "figures": [{"fig_id": "exp_fig_0", "caption": "c", "source": "experiment"}],
    "metrics": [
        {"name": "accuracy", "runs": [{"seq": 1, "value": 0.8}], "best": 0.8},
    ],
}


@pytest_asyncio.fixture(autouse=True)
async def _clean_crdt():
    from app.services.crdt_rooms import reset_crdt_rooms

    yield
    await reset_crdt_rooms()


@pytest_asyncio.fixture(autouse=True)
def _stub_tectonic(monkeypatch):
    """假 tectonic：用 pymupdf 产出真实可渲染的 main.pdf（评审渲染步骤走真路径）。"""

    def ok_run(binary: str, workdir: Path) -> TectonicRun:
        import pymupdf

        doc = pymupdf.open()
        page = doc.new_page()
        page.insert_text((72, 72), "Polaris compiled manuscript (test)")
        doc.save(workdir / "main.pdf")
        doc.close()
        (workdir / "main.log").write_text("", encoding="utf-8")
        return TectonicRun(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(latex_compile, "_find_tectonic", lambda: "/usr/bin/tectonic")
    monkeypatch.setattr(latex_compile, "_run_tectonic", ok_run)


def _make_engine() -> tuple[VoyageEngine, RecordingBus]:
    bus = RecordingBus()
    return VoyageEngine(event_bus=bus, llm_router=LLMRouter()), bus


async def _inject_intro(ms_id: str, text: str) -> None:
    """把测试正文注入 main.tex 的 introduction 分节。"""
    async with get_sessionmaker()() as session:
        stmt = select(ManuscriptFile).where(
            ManuscriptFile.manuscript_id == uuid.UUID(ms_id), ManuscriptFile.path == "main.tex"
        )
        file = (await session.execute(stmt)).scalar_one()
        file.content = file.content.replace(
            "% POLARIS_SECTION_END: introduction",
            f"{text}\n% POLARIS_SECTION_END: introduction",
        )
        await session.commit()


async def _prepare(client, *, intro: str, review_body=None):
    """项目 + 稿件 + 注入正文 + 编译 ok + 发起评审 → (project_id, headers, ms_id, voyage)。"""
    project_id, headers = await _setup_project(client)
    idea_id = await _seed_idea(project_id)
    exp_id = await _seed_experiment(project_id, idea_id)
    await _seed_paper(project_id, "Attention Is All You Need", 2017)
    resp = await _create_manuscript(
        client, headers, project_id, idea_id=idea_id, experiment_id=exp_id
    )
    ms_id = resp.json()["id"]
    await _inject_intro(ms_id, intro)
    resp = await client.post(f"/api/manuscripts/{ms_id}/compile", headers=headers)
    assert resp.status_code == 200 and resp.json()["status"] == "ok", resp.text
    resp = await client.post(f"/api/manuscripts/{ms_id}/review", json=review_body, headers=headers)
    assert resp.status_code == 201, resp.text
    return project_id, headers, ms_id, resp.json()


GOOD_INTRO = (
    r"Following \cite{smith2017attention}, our accuracy reaches 0.8 overall. "
    "This work builds on established retrieval results."
)


# ---- 发起端点 ----


async def test_review_endpoint_preconditions_and_conflict(client, queue_stub):
    project_id, headers = await _setup_project(client)
    resp = await _create_manuscript(client, headers, project_id)
    ms_id = resp.json()["id"]

    # 前置：无 ok 编译 → 409 COMPILE_REQUIRED
    resp = await client.post(f"/api/manuscripts/{ms_id}/review", headers=headers)
    assert resp.status_code == 409 and resp.json()["detail"] == "COMPILE_REQUIRED"

    resp = await client.post(f"/api/manuscripts/{ms_id}/compile", headers=headers)
    assert resp.json()["status"] == "ok"
    resp = await client.post(
        f"/api/manuscripts/{ms_id}/review",
        json={"personas": [{"name": "自定义评审员", "stance": "看重理论完备性"}]},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    voyage = resp.json()
    assert voyage["kind"] == "paper_review"
    assert ("run_voyage", (voyage["id"],), {}) in queue_stub.jobs

    # 同 manuscript 互斥 409
    resp = await client.post(f"/api/manuscripts/{ms_id}/review", headers=headers)
    assert resp.status_code == 409 and resp.json()["detail"] == "REVIEW_IN_PROGRESS"


# ---- 全链路：通过 ----


async def test_review_full_pipeline_pass(client, queue_stub):
    _, headers, ms_id, voyage = await _prepare(client, intro=GOOD_INTRO)
    engine, _ = _make_engine()
    await engine.run(uuid.UUID(voyage["id"]))

    run = (await client.get(f"/api/voyages/{voyage['id']}", headers=headers)).json()
    assert run["status"] == "done", run
    assert [s["action"] for s in run["steps"]] == [
        "review.citation_check",
        "review.fact_check",
        "review.render",
        "review.referees",
        "review.meta_review",
        "review.guardrail",
    ]
    assert run["steps"][2]["observation"]["pages"] >= 1  # 真 PDF 渲染出 PNG
    assert run["steps"][3]["observation"]["with_images"] >= 1  # 多模态输入

    # 评审历史（契约 §4）：meta 摘要 + 消息数（3 评审员 + 主席 Meta）
    reviews = (await client.get(f"/api/manuscripts/{ms_id}/reviews", headers=headers)).json()
    assert len(reviews) == 1
    summary = reviews[0]
    assert summary["passed"] is True
    assert summary["message_count"] == 4
    meta = summary["meta"]
    # 默认三人设 6/8/7，中位 7；低 confidence(2) 降权 0.5 → (6+8+3.5)/2.5 = 7.0
    assert meta["rating"] == 7.0
    assert meta["decision_hint"] == "accept"
    assert meta["aggregation"]["method"] == "median-outlier-suppressed"
    assert sorted(meta["aggregation"]["ratings"]) == [6.0, 7.0, 8.0]
    assert 1 <= meta["soundness"] <= 4

    # session payload：核验/查错/guardrail 契约 shape
    async with get_sessionmaker()() as session:
        review_session = await session.get(ReviewSession, uuid.UUID(summary["session_id"]))
        payload = review_session.payload
        assert review_session.status == "closed"
    check = payload["citation_check"]
    assert check["total"] == 1
    item = check["items"][0]
    assert item["bibkey"] == "smith2017attention"
    assert item["existence"] == "exact" and item["source"] == "library"
    assert item["support"] == "supported"
    assert item["matched_title"] == "Attention Is All You Need"
    assert "smith2017attention" in item["context_snippet"]
    assert payload["fact_check"]["items"] == []  # 数字 0.8 命中 metrics，无问题
    assert payload["guardrail"] == {"passed": True, "regenerated": 0}

    # 消息：逐评审员 + 主席 Meta（复用 sessions 端点，manuscript 权限走通）
    messages = (
        await client.get(f"/api/sessions/{summary['session_id']}/messages", headers=headers)
    ).json()
    authors = [m["author_name"] for m in messages]
    assert authors == ["苛刻方法论者", "建设性领域专家", "严格实验复现者", "主席 Meta"]
    assert all(m["author_type"] == "agent" for m in messages)

    # 稿件：review_passed=true、状态保持 compiled
    detail = (await client.get(f"/api/manuscripts/{ms_id}", headers=headers)).json()
    assert detail["review_passed"] is True
    assert detail["status"] == "compiled"
    assert "revision_notes" not in (detail["fact_pack"] or {})


# ---- 全链路：不通过（revision_notes + 状态回退） ----


async def test_review_fail_writes_revision_notes_and_rolls_back_status(client, queue_stub):
    _, headers, ms_id, voyage = await _prepare(client, intro=GOOD_INTRO + "\n% REVIEW_FAIL_TEST")
    # 模拟投稿审批中（under_review）被评审否决的回退路径
    async with get_sessionmaker()() as session:
        ms = await session.get(Manuscript, uuid.UUID(ms_id))
        ms.status = "under_review"
        await session.commit()

    engine, bus = _make_engine()
    await engine.run(uuid.UUID(voyage["id"]))
    run = (await client.get(f"/api/voyages/{voyage['id']}", headers=headers)).json()
    assert run["status"] == "done"

    reviews = (await client.get(f"/api/manuscripts/{ms_id}/reviews", headers=headers)).json()
    assert reviews[0]["passed"] is False
    assert reviews[0]["meta"]["rating"] == 3.0  # REVIEW_FAIL_TEST 全员低分
    assert reviews[0]["meta"]["decision_hint"] == "reject"

    detail = (await client.get(f"/api/manuscripts/{ms_id}", headers=headers)).json()
    assert detail["review_passed"] is False
    assert detail["status"] == "compiled"  # under_review 回 compiled
    notes = detail["fact_pack"]["revision_notes"]
    assert "评审修订说明" in notes and "实验规模有限" in notes
    statuses = [m["status"] for _, m in bus.notify if m.get("type") == "manuscript.status"]
    assert "compiled" in statuses  # WS manuscript.status

    # 不通过后 submit 仍被拦（REVIEW_REQUIRED）
    resp = await client.post(f"/api/manuscripts/{ms_id}/submit", headers=headers)
    assert resp.status_code == 409 and resp.json()["detail"] == "REVIEW_REQUIRED"


# ---- fabricated 强制不通过 + 支撑性标记 ----


async def test_fabricated_citation_forces_fail(client, queue_stub):
    intro = (
        "UNSUPPORTED_TEST claim precedes the citation. "
        + GOOD_INTRO
        + r" Prior art \cite{ghost2020nonexistent} pioneered this."
    )
    _, headers, ms_id, voyage = await _prepare(client, intro=intro)
    engine, _ = _make_engine()
    await engine.run(uuid.UUID(voyage["id"]))

    reviews = (await client.get(f"/api/manuscripts/{ms_id}/reviews", headers=headers)).json()
    summary = reviews[0]
    # 评分照常 7.0，但存在 fabricated → 强制不通过、判 reject
    assert summary["meta"]["rating"] == 7.0
    assert summary["meta"]["decision_hint"] == "reject"
    assert summary["passed"] is False

    async with get_sessionmaker()() as session:
        review_session = await session.get(ReviewSession, uuid.UUID(summary["session_id"]))
        items = {i["bibkey"]: i for i in review_session.payload["citation_check"]["items"]}
    ghost = items["ghost2020nonexistent"]
    assert ghost["existence"] == "fabricated"
    assert ghost["source"] == "none" and ghost["support"] == "not_checked"
    # 语境含 UNSUPPORTED_TEST 标记 → fake 支撑性判 unsupported
    assert items["smith2017attention"]["support"] == "unsupported"

    detail = (await client.get(f"/api/manuscripts/{ms_id}", headers=headers)).json()
    assert detail["review_passed"] is False
    assert "ghost2020nonexistent" in detail["fact_pack"]["revision_notes"]


# ---- guardrail：拒绝 → 重生成 → unreliable 排除聚合 ----


async def test_guardrail_regenerate_then_unreliable(client, queue_stub):
    _, headers, ms_id, voyage = await _prepare(client, intro=GOOD_INTRO + "\n% GUARDRAIL_FAIL_TEST")
    engine, _ = _make_engine()
    await engine.run(uuid.UUID(voyage["id"]))

    run = (await client.get(f"/api/voyages/{voyage['id']}", headers=headers)).json()
    assert run["status"] == "done"
    obs = next(s["observation"] for s in run["steps"] if s["action"] == "review.referees")
    assert obs["unreliable"] == 1 and obs["regenerated"] == 2

    reviews = (await client.get(f"/api/manuscripts/{ms_id}/reviews", headers=headers)).json()
    summary = reviews[0]
    async with get_sessionmaker()() as session:
        review_session = await session.get(ReviewSession, uuid.UUID(summary["session_id"]))
        payload = review_session.payload
    by_persona = {r["persona"]: r for r in payload["reviews"]}
    assert by_persona["严格实验复现者"]["unreliable"] is True
    assert by_persona["严格实验复现者"]["regenerated"] == 2
    # 排除 unreliable 后聚合（6+8）/2 = 7.0，仍通过；guardrail.passed=false 记录重生成
    assert payload["guardrail"] == {"passed": False, "regenerated": 2}
    assert summary["meta"]["rating"] == 7.0
    assert summary["meta"]["aggregation"]["ratings"] == [6.0, 8.0]
    assert summary["passed"] is True

    # unreliable 的意见仍发布为消息（灰显由前端处理）
    messages = (
        await client.get(f"/api/sessions/{summary['session_id']}/messages", headers=headers)
    ).json()
    unreliable_msg = next(m for m in messages if m["author_name"] == "严格实验复现者")
    assert "unreliable" in unreliable_msg["content"]


# ---- 核验三态（respx mock S2/OpenAlex） ----


def _s2_search_payload(title: str, year: int) -> dict:
    return {"data": [{"title": title, "year": year, "authors": []}]}


@respx.mock
async def test_citation_existence_three_states_via_s2(client):
    respx.get(url__regex=r"https://api\.semanticscholar\.org/graph/v1/paper/search.*").mock(
        side_effect=lambda request: httpx.Response(
            200,
            json=(
                _s2_search_payload("Deep Retrieval Advances", 2020)
                if "Deep" in str(request.url)
                else _s2_search_payload("Graph Networks Overview", 2022)
                if "Graph" in str(request.url)
                else {"data": []}
            ),
        )
    )
    respx.get(url__regex=r"https://api\.openalex\.org/works.*").mock(
        return_value=httpx.Response(200, json={"results": []})
    )
    fact_citations = [
        {"bibkey": "a2020deep", "title": "Deep Retrieval Advances", "year": 2020, "source": "s2"},
        {"bibkey": "b2018graph", "title": "Graph Networks Overview", "year": 2018, "source": "s2"},
        {"bibkey": "c2021void", "title": "Totally Fabricated Work", "year": 2021, "source": "s2"},
    ]
    cited = [
        {"bibkey": k, "location": "main.tex:1", "context_snippet": "ctx"}
        for k in ("a2020deep", "b2018graph", "c2021void")
    ]
    s2 = SemanticScholarClient(rate=1000.0)
    openalex = OpenAlexClient()
    try:
        async with get_sessionmaker()() as session:
            items = await pr.check_citation_existence(
                session, cited, fact_citations, s2=s2, openalex=openalex
            )
    finally:
        await s2.aclose()
        await openalex.aclose()
    by_key = {i["bibkey"]: i for i in items}
    # 同题同年 → exact/s2；同题年差 4（>容差 1）→ minor/s2；无命中 → fabricated/none
    assert by_key["a2020deep"]["existence"] == "exact"
    assert by_key["a2020deep"]["source"] == "s2"
    assert by_key["a2020deep"]["matched_title"] == "Deep Retrieval Advances"
    assert by_key["b2018graph"]["existence"] == "minor"
    assert by_key["b2018graph"]["source"] == "s2"
    assert by_key["c2021void"]["existence"] == "fabricated"
    assert by_key["c2021void"]["source"] == "none"


@respx.mock
async def test_citation_check_openalex_fallback(client):
    """S2 不可达（500）→ OpenAlex 命中 → source=openalex；双端失败 → 保守 minor。"""
    respx.get(url__regex=r"https://api\.semanticscholar\.org/graph/v1/paper/search.*").mock(
        return_value=httpx.Response(500)
    )
    oa_route = respx.get(url__regex=r"https://api\.openalex\.org/works.*").mock(
        return_value=httpx.Response(
            200,
            json={"results": [{"title": "Deep Retrieval Advances", "publication_year": 2020}]},
        )
    )
    entry = {"bibkey": "a2020deep", "title": "Deep Retrieval Advances", "year": 2020}
    s2 = SemanticScholarClient(rate=1000.0)
    openalex = OpenAlexClient()
    try:
        existence, matched, source = await pr.match_citation_remote(entry, s2=s2, openalex=openalex)
        assert (existence, matched, source) == ("exact", "Deep Retrieval Advances", "openalex")

        oa_route.mock(return_value=httpx.Response(500))
        existence, matched, source = await pr.match_citation_remote(
            {"bibkey": "x", "title": "Another Unreachable Paper", "year": 2021},
            s2=s2,
            openalex=openalex,
        )
        # 双端不可达：不诬告 fabricated，保守 minor/none
        assert (existence, source) == ("minor", "none")
    finally:
        await s2.aclose()
        await openalex.aclose()


# ---- 查错：数字比对 / \ref / 图存在性（确定性单测） ----


def test_scan_fact_issues_deterministic():
    files = [
        (
            "main.tex",
            "accuracy reaches 0.8 overall\n"  # 命中 metrics
            "improves to 80%\n"  # 80% ≙ 0.8
            "proposed in 2017 with 3 seeds\n"  # 白名单：年份 / 小整数
            "as shown in Section 12\n"  # 白名单：章节引用
            "accuracy reaches 0.93 overall\n"  # 未命中 → number_mismatch
            "misleading gain of 93.5%\n"  # 未命中 → number_mismatch
            "% comment with 0.123 ignored\n"
            "see Figure~\\ref{fig:main} and \\ref{sec:missing}\n"
            "\\label{fig:main}\n"
            "\\includegraphics[width=0.9\\linewidth]{figures/exp_fig_0.pdf}\n"
            "\\includegraphics{figures/not_a_fig.pdf}\n",
        )
    ]
    items = pr.scan_fact_issues(files, FACT_PACK)
    kinds = sorted((i["kind"], i["severity"]) for i in items)
    assert kinds == [
        ("missing_figure", "major"),
        ("number_mismatch", "major"),
        ("number_mismatch", "major"),
        ("other", "minor"),
    ]
    issues = "\n".join(i["issue"] for i in items)
    assert "0.93" in issues and "93.5%" in issues
    assert "sec:missing" in issues and "fig:main" not in issues
    assert "not_a_fig" in issues and "exp_fig_0" not in issues
    locations = {i["location"] for i in items}
    assert all(loc.startswith("main.tex:") for loc in locations)


# ---- reviewer JSON 校验与聚合（纯函数单测） ----


def test_validate_reviewer_json_strict():
    valid = {
        "soundness": 3,
        "presentation": 2,
        "contribution": 4,
        "rating": 7,
        "confidence": 4,
        "strengths": ["具体优点"],
        "weaknesses": ["具体不足"],
        "questions": [],
    }
    out = pr.validate_reviewer_json(valid)
    assert out["rating"] == 7.0 and out["strengths"] == ["具体优点"]

    import pytest

    for bad in (
        valid | {"rating": 11},
        valid | {"soundness": 5},
        valid | {"confidence": 0},
        valid | {"strengths": "not-a-list"},
        valid | {"strengths": [], "weaknesses": []},
        "not-a-dict",
    ):
        with pytest.raises(ValueError):
            pr.validate_reviewer_json(bad)


def test_aggregate_reviews_median_and_downweight():
    def review(rating, confidence=5.0, unreliable=False):
        return {
            "soundness": 3.0,
            "presentation": 3.0,
            "contribution": 3.0,
            "rating": rating,
            "confidence": confidence,
            "unreliable": unreliable,
        }

    # 中位 7；离群 2（|2-7|>3）降权 0.5 → (1+7+8)/2.5 = 6.4
    agg = pr.aggregate_reviews([review(2.0), review(7.0), review(8.0)])
    assert agg["aggregation"]["median"] == 7.0
    assert agg["aggregation"]["weights"] == [0.5, 1.0, 1.0]
    assert agg["rating"] == 6.4

    # 低 confidence(≤2) 降权；两规则叠乘
    agg = pr.aggregate_reviews([review(2.0, confidence=2.0), review(7.0), review(8.0)])
    assert agg["aggregation"]["weights"] == [0.25, 1.0, 1.0]

    # unreliable 不计入聚合
    agg = pr.aggregate_reviews([review(1.0, unreliable=True), review(6.0), review(8.0)])
    assert agg["aggregation"]["ratings"] == [6.0, 8.0]
    assert agg["rating"] == 7.0

    # 全员 unreliable → 0 分兜底
    agg = pr.aggregate_reviews([review(9.0, unreliable=True)])
    assert agg["rating"] == 0.0 and agg["aggregation"]["ratings"] == []

    # decision_hint / review_passed 判定
    assert pr.decision_hint(7.0, has_fabricated=False, has_reliable=True) == "accept"
    assert pr.decision_hint(5.5, has_fabricated=False, has_reliable=True) == "borderline"
    assert pr.decision_hint(3.0, has_fabricated=False, has_reliable=True) == "reject"
    assert pr.decision_hint(9.0, has_fabricated=True, has_reliable=True) == "reject"
    assert pr.review_passed({"rating": 7.0}, {"items": []}) is True
    assert pr.review_passed({"rating": 7.0}, {"items": [{"existence": "fabricated"}]}) is False
    assert pr.review_passed({"rating": 5.9}, {"items": []}) is False

    # 人设解析：自定义不足三个用默认补齐
    personas = pr.resolve_review_personas([{"name": "自定义", "stance": "s"}])
    assert [p["name"] for p in personas] == ["自定义", "建设性领域专家", "严格实验复现者"]


# ---- submit 前置升级与 gate override ----


async def test_submit_review_required_and_gate_override(client, bus_recorder, queue_stub):
    project_id, headers = await _setup_project(client)
    resp = await _create_manuscript(client, headers, project_id)
    ms_id = resp.json()["id"]
    resp = await client.post(f"/api/manuscripts/{ms_id}/compile", headers=headers)
    assert resp.json()["status"] == "ok"

    # 编译 ok 但 review_passed=false → 409 REVIEW_REQUIRED
    resp = await client.post(f"/api/manuscripts/{ms_id}/submit", headers=headers)
    assert resp.status_code == 409 and resp.json()["detail"] == "REVIEW_REQUIRED"

    async with get_sessionmaker()() as session:
        ms = await session.get(Manuscript, uuid.UUID(ms_id))
        ms.review_passed = True
        await session.commit()
    resp = await client.post(f"/api/manuscripts/{ms_id}/submit", headers=headers)
    assert resp.status_code == 201, resp.text
    gate = resp.json()
    assert gate["payload"]["review_passed"] is True

    # 提交后评审被否（review_passed 回 false）→ 无 override 审批 409，闸门保持 pending
    async with get_sessionmaker()() as session:
        ms = await session.get(Manuscript, uuid.UUID(ms_id))
        ms.review_passed = False
        await session.commit()
    resp = await client.post(f"/api/gates/{gate['id']}/approve", json={}, headers=headers)
    assert resp.status_code == 409 and resp.json()["detail"] == "REVIEW_REQUIRED"

    # 管理员 override=true → 跳过前置，正常批准并联动 submitted
    resp = await client.post(
        f"/api/gates/{gate['id']}/approve", json={"override": True}, headers=headers
    )
    assert resp.status_code == 200, resp.text
    detail = (await client.get(f"/api/manuscripts/{ms_id}", headers=headers)).json()
    assert detail["status"] == "submitted"


# ---- 评审历史与 manuscript session 消息权限 ----


async def test_reviews_history_and_session_permissions(client, bus_recorder):
    project_id, headers = await _setup_project(client)
    resp = await _create_manuscript(client, headers, project_id)
    ms_id = resp.json()["id"]

    async with get_sessionmaker()() as session:
        review_session = ReviewSession(
            target_type="manuscript",
            target_id=uuid.UUID(ms_id),
            status="closed",
            payload={"passed": True, "meta": {"rating": 7.0, "decision_hint": "accept"}},
        )
        session.add(review_session)
        await session.flush()
        session.add(
            ReviewMessage(
                session_id=review_session.id,
                author_type="agent",
                author_name="主席 Meta",
                content="总评 7/10（accept）",
                round=1,
            )
        )
        await session.commit()
        sid = str(review_session.id)

    reviews = (await client.get(f"/api/manuscripts/{ms_id}/reviews", headers=headers)).json()
    assert len(reviews) == 1
    assert reviews[0]["session_id"] == sid
    assert reviews[0]["meta"]["rating"] == 7.0
    assert reviews[0]["message_count"] == 1

    # 成员：manuscript session 消息读写走通（人类讨论复用 M3 端点）
    resp = await client.get(f"/api/sessions/{sid}/messages", headers=headers)
    assert resp.status_code == 200 and len(resp.json()) == 1
    resp = await client.post(
        f"/api/sessions/{sid}/messages", json={"content": "这个评审结论我认可"}, headers=headers
    )
    assert resp.status_code == 201
    assert resp.json()["author_type"] == "human"

    # 非成员 404（不泄露存在性）
    outsider = await register_and_login(client, email="mallory@example.com")
    outsider_headers = {"Authorization": f"Bearer {outsider}"}
    resp = await client.get(f"/api/sessions/{sid}/messages", headers=outsider_headers)
    assert resp.status_code == 404
    resp = await client.get(f"/api/manuscripts/{ms_id}/reviews", headers=outsider_headers)
    assert resp.status_code == 404
