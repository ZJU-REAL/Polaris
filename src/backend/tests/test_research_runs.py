"""研究：一个方向从输入走到「等用户选定想法」。

- ``decide`` 的状态表逐行（设计 §1.3）与文献检索结果的分类逐行（§3.2）；
- 端到端：fake 模型 + 注入的 arXiv 客户端，POST 一个中文方向，跑到想法出现；
- 创建幂等（client_key、60 秒内同样的方向）；
- 中途重启：任务跑完但没来得及推进、建了任务没来得及记账，对账都能接上；
- 中途停止，再重试；
- arXiv 失败而别的来源有结果时检索继续，都失败时这一步失败；
- 手动节奏的库不进每日自动同步。
"""

import uuid
from types import SimpleNamespace

import fakeredis.aioredis
import httpx
import pytest
import pytest_asyncio
import respx
from sqlalchemy import select

from app.agents.voyage import actions_wiki
from app.agents.voyage.engine import VoyageEngine
from app.core.db import get_sessionmaker
from app.core.llm.errors import LLMAuthError, LLMOverloadedError
from app.core.llm.router import LLMRouter
from app.core.queue import InlineTaskQueue, get_task_queue
from app.models.base import utcnow
from app.models.idea import Idea
from app.models.library_direction import DirectionLibrary
from app.models.project import Project
from app.models.research_run import ResearchRun
from app.models.voyage import VoyageRun, VoyageStep
from app.services import ingest as ingest_service
from app.services import research_runs as research
from app.services.literature import (
    ArxivClient,
    OpenAlexClient,
    SemanticScholarClient,
    reset_clients,
    set_clients,
)
from app.services.research_runs import ChildFacts, Decision, Facts, classify_survey, decide
from tests.conftest import RecordingBus, StubQueue, register_and_login
from tests.test_wiki_ingest import _make_pdf_bytes
from worker import tasks as worker_tasks

DIRECTION = "可验证的 agent 工作流是否优于端到端生成"

# ---- 状态表（纯函数）：设计 §1.3 逐行 ----

ENOUGH = dict(papers_found=40, papers_kept=12, papers_indexed=12)


def _child(status, **kw):
    return ChildFacts(status=status, **kw)


def _code(decision: Decision) -> str | None:
    return (decision.attention or {}).get("code")


DECIDE_TABLE = [
    # (说明, phase, status, facts, 期望 (action, phase, status, attention code))
    (
        "plan：还没有简报 → 去确定范围",
        "plan",
        "running",
        Facts(),
        ("plan", "plan", "running", None),
    ),
    (
        "plan：试了三次都不行 → 需要处理（连不上模型）",
        "plan",
        "running",
        Facts(plan_attempts=3, plan_error={"code": "NETWORK", "transient": True}),
        ("none", "plan", "needs_attention", "LLM_UNREACHABLE"),
    ),
    (
        "plan：密钥不对 → 不等三次，直接需要处理",
        "plan",
        "running",
        Facts(plan_attempts=1, plan_error={"code": "AUTH", "transient": False, "fatal": True}),
        ("none", "plan", "needs_attention", "LLM_AUTH"),
    ),
    (
        "plan：三次都是别的原因 → PLAN_FAILED",
        "plan",
        "running",
        Facts(plan_attempts=3, plan_error={"code": "INVALID_OUTPUT", "transient": False}),
        ("none", "plan", "needs_attention", "PLAN_FAILED"),
    ),
    (
        "survey：任务在跑 → 不动",
        "survey",
        "running",
        Facts(child=_child("executing")),
        ("none", "survey", "running", None),
    ),
    (
        "survey：出错停着、自动重试还排着 → 仍算运行中",
        "survey",
        "running",
        Facts(child=_child("paused_error", retry_pending=True, error_transient=True)),
        ("none", "survey", "running", None),
    ),
    (
        "survey：任务完成、论文够 → 建寻找假设的任务",
        "survey",
        "running",
        Facts(child=_child("done", search_stats={"source_fetched": 40}), **ENOUGH),
        ("start_hypotheses", "survey", "running", None),
    ),
    (
        "survey：任务完成、论文不够 → 需要处理，绝不往下走",
        "survey",
        "running",
        Facts(
            child=_child("done", search_stats={"source_fetched": 38}),
            papers_found=38,
            papers_kept=2,
            papers_indexed=2,
        ),
        ("none", "survey", "needs_attention", "SURVEY_TOO_FEW_RELEVANT"),
    ),
    (
        "survey：出错停着、重试用完 → SURVEY_FAILED",
        "survey",
        "running",
        Facts(
            child=_child(
                "paused_error",
                error_code="INVALID_OUTPUT",
                error_message="执行步骤时出错",
                failed_action="wiki.compile",
            )
        ),
        ("none", "survey", "needs_attention", "SURVEY_FAILED"),
    ),
    (
        "survey：任务 failed → SURVEY_FAILED",
        "survey",
        "running",
        Facts(child=_child("failed")),
        ("none", "survey", "needs_attention", "SURVEY_FAILED"),
    ),
    (
        "survey：停在检索那一步、连不上、一篇没拿到 → 来源连不上",
        "survey",
        "running",
        Facts(
            child=_child(
                "paused_error",
                error_code="NETWORK",
                error_transient=True,
                failed_action="wiki.search_candidates",
            )
        ),
        ("none", "survey", "needs_attention", "SURVEY_SOURCES_UNREACHABLE"),
    ),
    (
        "survey：任务记录没了 → SURVEY_FAILED",
        "survey",
        "running",
        Facts(child=None),
        ("none", "survey", "needs_attention", "SURVEY_FAILED"),
    ),
    (
        "hypotheses：任务在问用户 → 等你回答",
        "hypotheses",
        "running",
        Facts(child=_child("paused_ask"), **ENOUGH),
        ("none", "hypotheses", "waiting_user", None),
    ),
    (
        "hypotheses：任务停在闸门 → 等你回答",
        "hypotheses",
        "running",
        Facts(child=_child("paused_gate"), **ENOUGH),
        ("none", "hypotheses", "waiting_user", None),
    ),
    (
        "hypotheses：任务完成、有想法 → 等你选定",
        "hypotheses",
        "running",
        Facts(child=_child("done"), ideas=4, **ENOUGH),
        ("none", "choose", "waiting_user", None),
    ),
    (
        "hypotheses：任务完成、一个想法都没有 → 需要处理",
        "hypotheses",
        "running",
        Facts(child=_child("done"), ideas=0, **ENOUGH),
        ("none", "hypotheses", "needs_attention", "NO_HYPOTHESES_SURVIVED"),
    ),
    (
        "hypotheses：出错停着（不是临时故障）→ HYPOTHESES_FAILED",
        "hypotheses",
        "running",
        Facts(child=_child("paused_error", error_code="INVALID_OUTPUT")),
        ("none", "hypotheses", "needs_attention", "HYPOTHESES_FAILED"),
    ),
    (
        "hypotheses：临时故障、自动重试用完 → 模型服务连不上",
        "hypotheses",
        "running",
        Facts(child=_child("paused_error", error_code="RATE_LIMITED", error_transient=True)),
        ("none", "hypotheses", "needs_attention", "LLM_UNREACHABLE"),
    ),
    (
        "survey：打分那一步临时故障、自动重试用完 → 模型服务连不上（不是来源连不上）",
        "survey",
        "running",
        Facts(
            child=_child(
                "paused_error",
                error_code="NETWORK",
                error_transient=True,
                failed_action="wiki.score_relevance",
                search_stats={"source_fetched": 20},
            ),
            papers_found=20,
            papers_unscreened=20,
        ),
        ("none", "survey", "needs_attention", "LLM_UNREACHABLE"),
    ),
    (
        "hypotheses：任务 failed → HYPOTHESES_FAILED",
        "hypotheses",
        "running",
        Facts(child=_child("failed")),
        ("none", "hypotheses", "needs_attention", "HYPOTHESES_FAILED"),
    ),
    (
        "hypotheses：出错是密钥问题 → LLM_AUTH",
        "hypotheses",
        "running",
        Facts(child=_child("paused_error", error_code="AUTH")),
        ("none", "hypotheses", "needs_attention", "LLM_AUTH"),
    ),
    (
        "任何阶段：任务被取消 → 研究也停止（survey）",
        "survey",
        "running",
        Facts(child=_child("cancelled")),
        ("none", "survey", "cancelled", None),
    ),
    (
        "任何阶段：任务被取消 → 研究也停止（hypotheses）",
        "hypotheses",
        "running",
        Facts(child=_child("cancelled")),
        ("none", "hypotheses", "cancelled", None),
    ),
    (
        "choose：选定了至少一个想法 → 已完成",
        "choose",
        "waiting_user",
        Facts(ideas=4, picked=1),
        ("none", "choose", "done", None),
    ),
    (
        "choose：还没选 → 等你选定",
        "choose",
        "waiting_user",
        Facts(ideas=4, picked=0),
        ("none", "choose", "waiting_user", None),
    ),
    (
        "choose：选了又全部取消 → 回到等你选定",
        "choose",
        "done",
        Facts(ideas=4, picked=0),
        ("none", "choose", "waiting_user", None),
    ),
    (
        "用户停止的：不管任务后来怎样都不动",
        "survey",
        "cancelled",
        Facts(child=_child("done", search_stats={"source_fetched": 40}), **ENOUGH),
        ("none", "survey", "cancelled", None),
    ),
    (
        "需要处理之后用户在任务页恢复了任务 → 回到运行中",
        "survey",
        "needs_attention",
        Facts(child=_child("executing")),
        ("none", "survey", "running", None),
    ),
]


@pytest.mark.parametrize(
    ("phase", "status", "facts", "expected"),
    [row[1:] for row in DECIDE_TABLE],
    ids=[row[0] for row in DECIDE_TABLE],
)
def test_decide_table(phase, status, facts, expected):
    decision = decide(phase, status, facts)
    assert (decision.action, decision.phase, decision.status, _code(decision)) == expected


def test_decide_is_stable_when_applied_twice():
    """把决定应用上去再问一次，答案不变（advance 可以随便重复调的前提）。"""
    for _name, phase, status, facts, _expected in DECIDE_TABLE:
        first = decide(phase, status, facts)
        if first.action != "none":
            continue
        again = decide(first.phase, first.status, facts)
        # 进了 choose 之后不再看任务，只看有没有选定
        assert (again.phase, again.status, _code(again)) == (
            first.phase,
            first.status,
            _code(first),
        )


# ---- 文献检索结果的分类：设计 §3.2 逐行 ----

SURVEY_TABLE = [
    (
        "一篇没拿到，每个来源都报错 → 来源连不上（重试）",
        {
            "source": "arxiv,openalex",
            "source_fetched": 0,
            "source_errors": [
                {"source": "arxiv", "error": "ConnectError"},
                {"source": "openalex", "error": "ReadTimeout"},
            ],
        },
        Facts(),
        ("SURVEY_SOURCES_UNREACHABLE", "retry"),
    ),
    (
        "一篇没拿到，来源没报错 → 没有结果（改关键词）",
        {"source": "arxiv,openalex", "source_fetched": 0, "source_errors": []},
        Facts(),
        ("SURVEY_NO_RESULTS", "edit_scope"),
    ),
    (
        "一篇没拿到，只有一部分来源报错 → 还是没有结果",
        {
            "source": "arxiv,openalex",
            "source_fetched": 0,
            "source_errors": [{"source": "arxiv", "error": "HTTPStatusError"}],
        },
        Facts(),
        ("SURVEY_NO_RESULTS", "edit_scope"),
    ),
    (
        "拿到了，但留下的不到 5 篇 → 相关的太少（放宽）",
        {"source": "arxiv", "source_fetched": 38},
        Facts(papers_found=38, papers_kept=2, papers_indexed=2),
        ("SURVEY_TOO_FEW_RELEVANT", "broaden"),
    ),
    (
        "留下的够，但建好索引的不到 5 篇 → 可检索的论文太少（重试）",
        {"source": "arxiv", "source_fetched": 38},
        Facts(papers_found=38, papers_kept=9, papers_indexed=3),
        ("LIBRARY_TOO_SMALL", "retry"),
    ),
    (
        "拿到了，但候选还没筛过（筛选没做完）→ 这一步没做完，不是「相关的太少」",
        {"source": "arxiv,openalex", "source_fetched": 20},
        Facts(papers_found=20, papers_kept=0, papers_unscreened=20),
        ("SURVEY_FAILED", "retry"),
    ),
    (
        "至少 5 篇能检索 → 继续",
        {"source": "arxiv", "source_fetched": 38},
        Facts(papers_found=38, papers_kept=9, papers_indexed=5),
        None,
    ),
    (
        "这次一篇没拿到，但库里本来就够（重做时）→ 继续",
        {"source": "arxiv", "source_fetched": 0},
        Facts(papers_found=20, papers_kept=9, papers_indexed=9),
        None,
    ),
    (
        "没有检索统计（存量任务），库是空的 → 没有结果",
        None,
        Facts(),
        ("SURVEY_NO_RESULTS", "edit_scope"),
    ),
]


@pytest.mark.parametrize(
    ("stats", "facts", "expected"),
    [row[1:] for row in SURVEY_TABLE],
    ids=[row[0] for row in SURVEY_TABLE],
)
def test_classify_survey_table(stats, facts, expected):
    problem = classify_survey(stats, facts)
    if expected is None:
        assert problem is None
        return
    assert (problem["code"], problem["action"]) == expected
    if problem["code"] == "SURVEY_TOO_FEW_RELEVANT":
        assert problem["counts"] == {"found": 38, "relevant": 2}  # 文案里的两个数
    if problem["code"] == "LIBRARY_TOO_SMALL":
        assert problem["counts"] == {"papers": 3, "required": 5}


# ---- 造环境：离线的 arXiv + 真的进程内队列 ----


def _feed(entries: list[tuple[str, str, str]]) -> str:
    body = "".join(
        f"""
  <entry>
    <id>http://arxiv.org/abs/{arxiv_id}v1</id>
    <title>{title}</title>
    <summary>{summary}</summary>
    <published>2026-10-01T00:00:00Z</published>
    <updated>2026-10-01T00:00:00Z</updated>
    <author><name>Author {i}</name></author>
    <category term="cs.CL"/>
  </entry>"""
        for i, (arxiv_id, title, summary) in enumerate(entries)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<feed xmlns="http://www.w3.org/2005/Atom" '
        f'xmlns:arxiv="http://arxiv.org/schemas/atom">{body}\n</feed>\n'
    )


SIX_RELEVANT = [
    (
        f"2610.0000{i}",
        f"Verifiable Agent Workflow Study {i}",
        f"Agent planning with verification workflow, variant {i}. 可验证的 agent 工作流。",
    )
    for i in range(1, 7)
] + [("2610.00009", "An irrelevant study of basket weaving", "Nothing here (irrelevant).")]


@pytest_asyncio.fixture
async def literature(app):
    """离线文献环境：arXiv 检索与 PDF 用 respx 顶替，其余外部请求一律 404。"""
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    set_clients(
        arxiv=ArxivClient(redis=redis, min_interval=0),
        s2=SemanticScholarClient(redis=redis, api_key="", rate=10_000, backoff_base=0.0),
        openalex=OpenAlexClient(redis=redis, mailto="test@example.org"),
    )
    state = {"feed": _feed(SIX_RELEVANT)}
    pdf_bytes = _make_pdf_bytes()
    with respx.mock(assert_all_called=False) as router:
        router.get(url__regex=r"https://export\.arxiv\.org/api/query.*").mock(
            side_effect=lambda request: httpx.Response(200, text=state["feed"])
        )
        router.get(url__regex=r"https://arxiv\.org/pdf/.*").mock(
            return_value=httpx.Response(200, content=pdf_bytes)
        )
        router.route().mock(return_value=httpx.Response(404, json={}))
        yield state
    reset_clients()
    await redis.aclose()


@pytest_asyncio.fixture
async def live_queue(app):
    """真的进程内队列：入队的任务就在本进程里跑，和桌面引擎一样。"""
    queue = InlineTaskQueue()
    app.dependency_overrides[get_task_queue] = lambda: queue
    yield queue
    await queue.shutdown(grace_s=1.0)


async def _login(client) -> dict:
    token = await register_and_login(client)
    return {"Authorization": f"Bearer {token}"}


async def _run_row(run_id: str) -> ResearchRun:
    async with get_sessionmaker()() as session:
        return await session.get(ResearchRun, uuid.UUID(run_id))


def _ctx(queue) -> dict:
    return {"redis": SimpleNamespace(enqueue_job=queue.enqueue)}


async def _advance(run_id: str, queue) -> ResearchRun:
    async with get_sessionmaker()() as session:
        return await research.advance(session, uuid.UUID(run_id), queue.enqueue)


def _engine() -> VoyageEngine:
    return VoyageEngine(event_bus=RecordingBus(), llm_router=LLMRouter())


# ---- 端到端 ----


async def test_one_direction_runs_from_post_to_ideas(client, live_queue, literature):
    headers = await _login(client)
    resp = await client.post(
        "/api/research", json={"direction": DIRECTION, "client_key": "k-e2e"}, headers=headers
    )
    assert resp.status_code == 201, resp.text
    created = resp.json()
    # 立刻返回：课题已建好、名字先用方向原文，库还没有
    assert created["phase"] == "plan" and created["status"] == "running"
    assert created["name"] == DIRECTION[:40] and created["library_id"] is None
    assert created["counts"] == {"papers": 0, "ideas": 0, "picked": 0}

    await live_queue.drain()  # 确定范围 → 检索文献 → 寻找假设，一路跑完

    detail = (await client.get(f"/api/research/{created['id']}", headers=headers)).json()
    assert detail["phase"] == "choose" and detail["status"] == "waiting_user", detail
    assert detail["attention"] is None

    # 简报：中文方向也给了英文关键词；不认识的学科、来源、分类、中文关键词都被丢掉
    brief = detail["brief"]
    assert brief["language"] == "zh"
    assert brief["include_keywords"] == [
        "agent planning",
        "verification workflow",
        "retrieval augmented generation",
    ]
    assert brief["arxiv_categories"] == ["cs.CL"]
    assert brief["discipline"] is None and brief["sources"] == ["arxiv"]
    assert brief["months_back"] == 36  # 模型给的 30 归到三档里最近的一档
    assert (brief["max_papers"], brief["compile_top_n"], brief["relevance_threshold"]) == (
        200,
        60,
        0.5,
    )
    # 课题改成了简报里的标题，库同名、回指课题、节奏是手动
    assert detail["name"] == brief["title"]
    async with get_sessionmaker()() as session:
        library = await session.get(DirectionLibrary, uuid.UUID(detail["library_id"]))
        assert library.name == brief["title"]
        assert str(library.project_id) == detail["project_id"]
        assert library.cadence == "manual" and library.definition["cadence"] == "manual"
        assert library.definition["keywords"]["include"] == brief["include_keywords"]
        assert library.definition["questions"] == brief["questions"]
        assert library not in await ingest_service.find_due_daily_libraries(session)
        survey = await session.get(VoyageRun, uuid.UUID(detail["survey"]["voyage_id"]))
        params = survey.checkpoint["params"]
        assert survey.kind == "wiki_bootstrap" and survey.status == "done"
        assert params["query_terms"] == brief["include_keywords"]
        assert params["knobs"]["relevance_threshold"] == 0.5
        assert params["knobs"]["months_back"] == 36
        discovery = await session.get(VoyageRun, uuid.UUID(detail["hypotheses"]["voyage_id"]))
        assert discovery.kind == "discovery" and discovery.status == "done"
        assert discovery.checkpoint["params"]["language"] == "zh"
        assert discovery.checkpoint["params"]["emit_ideas"] is True

    # 文献：进度从库里的数算出来
    survey = detail["survey"]
    assert survey["state"] == "done" and survey["step_title"] is None
    assert survey["step_action"] is None  # 任务结束后没有「正在做的一步」
    assert survey["progress"] == {
        "fetched": 28,  # 三条查询 + 一遍近期的，各回 7 条（去重前）
        "found": 7,  # 取进来筛选的：被筛掉的那篇也算，虽然它的成员行已经删掉
        "screened": 7,
        "kept": 6,
        "summarised": 6,
        "to_summarise": 6,
    }
    assert len(survey["top_papers"]) == 6
    assert survey["top_papers"][0]["title"].startswith("Verifiable Agent Workflow Study")
    assert survey["top_papers"][0]["relevance"] == 0.88 and survey["top_papers"][0]["tldr"]
    assert survey["sources"] == [
        {"source": "arxiv", "fetched": 28, "selected": 7, "error": None}
    ]
    # 每一条查询都按相关度排、各自留了记录
    assert [q["sort"] for q in survey["queries"]] == ["relevance"] * 4
    assert survey["queries"][0]["query"] == 'ti:"agent planning" OR abs:"agent planning"'
    assert all(q["returned"] == 7 and q["source"] == "arxiv" for q in survey["queries"])
    assert survey["usage"]["total_tokens"] > 0

    # 假设与想法
    hyp = detail["hypotheses"]
    assert hyp["state"] == "done" and hyp["max_rounds"] == 4 and hyp["round"] >= 1
    assert hyp["node_count"] >= 3
    assert detail["ideas"], "探索结束后必须有想法"
    idea = detail["ideas"][0]
    assert idea["status"] == "candidate"
    assert idea["origin"]["novelty_scope"] == "library"
    assert idea["source_voyage_id"] == hyp["voyage_id"]
    assert idea["evidence_counts"]["support"] >= 1
    assert idea["evidence_counts"]["unsupported"] == len(idea["origin"]["unsupported_subclaims"])
    assert detail["counts"] == {"papers": 6, "ideas": len(detail["ideas"]), "picked": 0}
    # latex 看本机有没有 tectonic（测试镜像里有），这里只钉形状
    assert detail["capabilities"]["remote_machine"] is False
    assert set(detail["capabilities"]) == {"remote_machine", "latex"}
    assert detail["open_ask"] is None and detail["next_retry_at"] is None
    assert detail["retry_attempt"] == 0 and detail["last_error"] is None
    assert survey["started_at"] and survey["finished_at"]
    assert hyp["started_at"] and hyp["finished_at"]
    assert detail["usage"]["total_tokens"] == (
        survey["usage"]["total_tokens"] + hyp["usage"]["total_tokens"]
    )
    assert detail["usage"]["cost_usd"] >= 0

    # 列表
    listed = (await client.get("/api/research", headers=headers)).json()
    assert [r["id"] for r in listed] == [created["id"]]
    assert listed[0]["phase"] == "choose" and listed[0]["counts"]["ideas"] == len(detail["ideas"])

    # 选定一个想法 → 下一次读就是已完成（不等对账）；取消选定 → 回到等你选定
    async def _set_idea(status: str) -> None:
        async with get_sessionmaker()() as session:
            (await session.get(Idea, uuid.UUID(idea["id"]))).status = status
            await session.commit()

    await _set_idea("promoted")
    after = (await client.get(f"/api/research/{created['id']}", headers=headers)).json()
    assert after["status"] == "done" and after["counts"]["picked"] == 1
    assert (await client.get("/api/research", headers=headers)).json()[0]["status"] == "done"
    await _set_idea("candidate")
    after = (await client.get(f"/api/research/{created['id']}", headers=headers)).json()
    assert after["status"] == "waiting_user" and after["counts"]["picked"] == 0
    await _set_idea("promoted")
    await worker_tasks.reconcile_research_runs(_ctx(live_queue))
    assert (await _run_row(created["id"])).status == "done"  # 对账把存的那份也跟上

    # 再生成一批：新任务、回到寻找假设；与已有想法重复的不重复写
    resp = await client.post(
        f"/api/research/{created['id']}/more-ideas", json={"rounds": 1}, headers=headers
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["phase"] == "hypotheses" and resp.json()["status"] == "running"
    await live_queue.drain()
    final = (await client.get(f"/api/research/{created['id']}", headers=headers)).json()
    assert final["phase"] == "choose" and final["status"] == "done"
    assert final["hypotheses"]["voyage_id"] != hyp["voyage_id"]
    assert final["hypotheses"]["max_rounds"] == 1
    row = await _run_row(created["id"])
    assert row.phases["hypotheses"]["history"] == [hyp["voyage_id"]]
    assert row.phases["hypotheses"]["attempts"] == 2

    # 改名：课题和库一起改
    resp = await client.patch(
        f"/api/research/{created['id']}", json={"name": "新名字"}, headers=headers
    )
    assert resp.json()["name"] == "新名字"
    async with get_sessionmaker()() as session:
        assert (await session.get(DirectionLibrary, uuid.UUID(detail["library_id"]))).name == (
            "新名字"
        )


async def test_brief_preview_creates_nothing(client, queue_stub):
    headers = await _login(client)
    resp = await client.post("/api/research/brief", json={"direction": DIRECTION}, headers=headers)
    assert resp.status_code == 200, resp.text
    brief = resp.json()
    assert brief["title"].endswith("（fake 课题）") and brief["include_keywords"]
    async with get_sessionmaker()() as session:
        assert (await session.execute(select(Project))).scalars().all() == []
        assert (await session.execute(select(ResearchRun))).scalars().all() == []
    assert queue_stub.jobs == []


# ---- 创建幂等 ----


async def test_create_is_idempotent_by_client_key_and_recent_same_text(client, queue_stub):
    headers = await _login(client)
    first = await client.post(
        "/api/research", json={"direction": DIRECTION, "client_key": "k1"}, headers=headers
    )
    assert first.status_code == 201
    run_id = first.json()["id"]
    assert queue_stub.jobs == [("research_advance", (run_id,), {"_job_id": f"research-{run_id}"})]

    # 同一个 key 再来（哪怕方向文字不同）：200 + 原来那一条，不再入队
    again = await client.post(
        "/api/research", json={"direction": "别的方向", "client_key": "k1"}, headers=headers
    )
    assert again.status_code == 200 and again.json()["id"] == run_id
    assert again.json()["direction"] == DIRECTION

    # 没带 key、60 秒内同样的方向（空白和大小写不同）：还是那一条
    dup = await client.post(
        "/api/research", json={"direction": f"  {DIRECTION.upper()}  "}, headers=headers
    )
    assert dup.status_code == 200 and dup.json()["id"] == run_id
    assert len(queue_stub.jobs) == 1

    # 不同的方向：新的一条
    other = await client.post("/api/research", json={"direction": "另一个方向"}, headers=headers)
    assert other.status_code == 201 and other.json()["id"] != run_id

    # 超过 60 秒：同样的方向也是新的一条
    async with get_sessionmaker()() as session:
        row = await session.get(ResearchRun, uuid.UUID(run_id))
        row.created_at = utcnow() - research.timedelta(seconds=61)
        await session.commit()
    later = await client.post("/api/research", json={"direction": DIRECTION}, headers=headers)
    assert later.status_code == 201 and later.json()["id"] not in (run_id, other.json()["id"])

    # 停止过的那一条不算：同样的方向可以马上重新开始
    await client.post(f"/api/research/{later.json()['id']}/cancel", headers=headers)
    fresh = await client.post("/api/research", json={"direction": DIRECTION}, headers=headers)
    assert fresh.status_code == 201

    async with get_sessionmaker()() as session:
        assert len((await session.execute(select(Project))).scalars().all()) == 4

    for bad in ("   ", "", "长" * 2001):
        resp = await client.post("/api/research", json={"direction": bad}, headers=headers)
        assert resp.status_code == 422 and resp.json()["detail"] == "DIRECTION_INVALID"
    resp = await client.get(f"/api/research/{uuid.uuid4()}", headers=headers)
    assert resp.status_code == 404 and resp.json()["detail"] == "RESEARCH_RUN_NOT_FOUND"


# ---- 确定范围 ----


async def test_user_brief_is_used_as_given_and_partial_brief_overlays(
    client, queue_stub, monkeypatch
):
    headers = await _login(client)
    calls = 0
    from app.core.llm.fake import FakeProvider

    real = FakeProvider._respond_research_brief

    def _count(last_user: str) -> str:
        nonlocal calls
        calls += 1
        return real(last_user)

    monkeypatch.setattr(FakeProvider, "_respond_research_brief", staticmethod(_count))

    # 完整的简报：原样采用，不调模型
    full = {
        "title": "我自己起的名字",
        "statement": "我自己写的范围。",
        "include_keywords": ["joint training retriever generator"],
        "months_back": 100,
        "max_papers": 40,
        "compile_top_n": 15,
    }
    resp = await client.post(
        "/api/research", json={"direction": DIRECTION, "brief": full}, headers=headers
    )
    run_id = resp.json()["id"]
    run = await _advance(run_id, queue_stub)
    assert calls == 0
    assert run.phase == "survey" and run.status == "running"
    assert run.brief["title"] == "我自己起的名字"
    assert run.brief["months_back"] == 100  # 用户填的不归档
    assert (run.brief["max_papers"], run.brief["compile_top_n"]) == (40, 15)
    async with get_sessionmaker()() as session:
        voyage = await session.get(VoyageRun, uuid.UUID(run.phases["survey"]["voyage_id"]))
        assert voyage.checkpoint["params"]["knobs"]["max_papers"] == 40
        assert voyage.checkpoint["params"]["knobs"]["compile_top_n"] == 15
        assert voyage.checkpoint["params"]["knobs"]["months_back"] == 100
        assert (await session.get(Project, run.project_id)).name == "我自己起的名字"
    assert ("run_voyage", (run.phases["survey"]["voyage_id"],), {}) in queue_stub.jobs

    # 只改了规模：其余由模型推，用户填的覆盖上去
    resp = await client.post(
        "/api/research",
        json={"direction": "另一个方向", "brief": {"max_papers": 30}},
        headers=headers,
    )
    run = await _advance(resp.json()["id"], queue_stub)
    assert calls == 1
    assert run.brief["max_papers"] == 30 and run.brief["compile_top_n"] == 60
    assert run.brief["title"].endswith("（fake 课题）")


async def test_planning_keeps_a_name_the_user_already_changed(client, queue_stub):
    headers = await _login(client)
    run_id = (
        await client.post("/api/research", json={"direction": DIRECTION}, headers=headers)
    ).json()["id"]
    await client.patch(f"/api/research/{run_id}", json={"name": "用户改的名字"}, headers=headers)
    run = await _advance(run_id, queue_stub)
    async with get_sessionmaker()() as session:
        assert (await session.get(Project, run.project_id)).name == "用户改的名字"
        assert (await session.get(DirectionLibrary, run.library_id)).name == "用户改的名字"


@pytest.fixture
def failing_llm():
    """一个可以点名「下一次调用失败」的替身模型，装在全局路由上。"""
    from app.core.llm.fake import FakeProvider
    from app.core.llm.router import get_llm_router, reset_llm_router

    fake = FakeProvider()
    get_llm_router().override_provider(fake)
    yield fake
    reset_llm_router()


async def test_planning_retries_three_times_then_needs_attention(client, queue_stub, failing_llm):
    headers = await _login(client)
    run_id = (
        await client.post("/api/research", json={"direction": DIRECTION}, headers=headers)
    ).json()["id"]
    provider = failing_llm
    for attempt in (1, 2):
        provider.fail_next(LLMOverloadedError("overloaded"))
        run = await _advance(run_id, queue_stub)
        assert (run.phase, run.status) == ("plan", "running")  # 等下一次对账再试
        assert run.phases["plan"]["attempts"] == attempt
        assert run.phases["plan"]["last_error"]["transient"] is True
    provider.fail_next(LLMOverloadedError("overloaded"))
    run = await _advance(run_id, queue_stub)
    assert run.status == "needs_attention"
    assert run.attention["code"] == "LLM_UNREACHABLE" and run.attention["action"] == "retry"
    # 需要处理之后对账不再自己去试
    run = await _advance(run_id, queue_stub)
    assert run.phases["plan"]["attempts"] == 3 and run.status == "needs_attention"
    async with get_sessionmaker()() as session:
        assert (await session.execute(select(VoyageRun))).scalars().all() == []

    # 用户点重试：次数清零，重新排上
    resp = await client.post(f"/api/research/{run_id}/retry", headers=headers)
    assert resp.status_code == 200 and resp.json()["status"] == "running"
    assert queue_stub.jobs[-1][0] == "research_advance"
    run = await _advance(run_id, queue_stub)
    assert run.phase == "survey" and run.attention is None


async def test_planning_stops_at_once_on_a_bad_key(client, queue_stub, failing_llm):
    headers = await _login(client)
    run_id = (
        await client.post("/api/research", json={"direction": DIRECTION}, headers=headers)
    ).json()["id"]
    failing_llm.fail_next(LLMAuthError("401"))
    run = await _advance(run_id, queue_stub)
    assert run.status == "needs_attention" and run.attention["code"] == "LLM_AUTH"
    assert run.phases["plan"]["attempts"] == 1
    detail = (await client.get(f"/api/research/{run_id}", headers=headers)).json()
    assert detail["last_error"]["source"] == "planner"

    # 预览简报时模型不可用：424 + 稳定的错误码（502–504 留给「连不上引擎」）
    failing_llm.fail_next(LLMAuthError("401"))
    resp = await client.post("/api/research/brief", json={"direction": DIRECTION}, headers=headers)
    assert resp.status_code == 424 and resp.json()["detail"] == "LLM_AUTH"
    failing_llm.fail_next(LLMOverloadedError("529"))
    resp = await client.post("/api/research/brief", json={"direction": DIRECTION}, headers=headers)
    assert resp.status_code == 424 and resp.json()["detail"] == "LLM_UNREACHABLE"


# ---- 中途重启 ----


async def test_restart_between_phases_is_picked_up_by_reconcile(client, queue_stub, literature):
    """引擎在文献检索任务跑完之后、推进研究之前死掉：重启后的对账接着建寻找假设的任务。
    再对一次账不会建第二条。"""
    headers = await _login(client)
    run_id = (
        await client.post("/api/research", json={"direction": DIRECTION}, headers=headers)
    ).json()["id"]
    run = await _advance(run_id, queue_stub)
    survey_id = uuid.UUID(run.phases["survey"]["voyage_id"])
    await _engine().run(survey_id)  # 任务跑完了，但没有人调收尾（= 进程死在这里）
    assert (await _run_row(run_id)).phase == "survey"

    seen = await worker_tasks.reconcile_research_runs(_ctx(queue_stub))
    assert seen == [run_id]
    run = await _run_row(run_id)
    assert (run.phase, run.status) == ("hypotheses", "running")
    discovery_id = run.phases["hypotheses"]["voyage_id"]
    assert ("run_voyage", (discovery_id,), {}) in queue_stub.jobs

    await worker_tasks.reconcile_research_runs(_ctx(queue_stub))
    async with get_sessionmaker()() as session:
        discoveries = (
            (await session.execute(select(VoyageRun).where(VoyageRun.kind == "discovery")))
            .scalars()
            .all()
        )
    assert [str(v.id) for v in discoveries] == [discovery_id]

    # 寻找假设的任务跑完时，驱动的收尾直接推进（不用等对账）
    await worker_tasks.run_voyage(_ctx(queue_stub), discovery_id)
    run = await _run_row(run_id)
    assert (run.phase, run.status) == ("choose", "waiting_user")


async def test_restart_inside_planning_adopts_what_was_already_created(
    client, queue_stub, literature
):
    """确定范围做到一半断掉（库和检索任务都建了，还没记到研究上）：再来一次认领
    已有的库和任务，不重复建。"""
    headers = await _login(client)
    run_id = (
        await client.post("/api/research", json={"direction": DIRECTION}, headers=headers)
    ).json()["id"]
    run = await _advance(run_id, queue_stub)
    library_id, survey_id = run.library_id, run.phases["survey"]["voyage_id"]
    # 回到「什么都没记」的样子
    async with get_sessionmaker()() as session:
        row = await session.get(ResearchRun, uuid.UUID(run_id))
        row.phase, row.library_id = "plan", None
        row.phases = {"plan": dict(row.phases["plan"])}
        await session.commit()

    run = await _advance(run_id, queue_stub)
    assert run.phase == "survey" and run.library_id == library_id
    assert run.phases["survey"]["voyage_id"] == survey_id
    async with get_sessionmaker()() as session:
        assert len((await session.execute(select(DirectionLibrary))).scalars().all()) == 1
        assert len((await session.execute(select(VoyageRun))).scalars().all()) == 1


# ---- 中途停止 ----


async def test_cancel_mid_survey_then_retry(client, queue_stub, literature):
    headers = await _login(client)
    run_id = (
        await client.post("/api/research", json={"direction": DIRECTION}, headers=headers)
    ).json()["id"]
    run = await _advance(run_id, queue_stub)
    survey_id = run.phases["survey"]["voyage_id"]

    resp = await client.post(f"/api/research/{run_id}/cancel", headers=headers)
    assert resp.status_code == 200 and resp.json()["status"] == "cancelled"
    async with get_sessionmaker()() as session:
        assert (await session.get(VoyageRun, uuid.UUID(survey_id))).status == "cancelled"
    # 再停一次：409；对账不会把它拉起来
    again = await client.post(f"/api/research/{run_id}/cancel", headers=headers)
    assert again.status_code == 409 and again.json()["detail"] == "RESEARCH_ALREADY_STOPPED"
    assert await worker_tasks.reconcile_research_runs(_ctx(queue_stub)) == []
    assert (await _run_row(run_id)).status == "cancelled"
    detail = (await client.get(f"/api/research/{run_id}", headers=headers)).json()
    assert detail["survey"]["state"] == "cancelled" and detail["survey"]["finished_at"]

    # 重试：新建一条检索任务，旧的进 history
    resp = await client.post(f"/api/research/{run_id}/retry", headers=headers)
    assert resp.status_code == 200 and resp.json()["status"] == "running"
    run = await _run_row(run_id)
    assert run.phases["survey"]["voyage_id"] != survey_id
    assert run.phases["survey"]["history"] == [survey_id]
    assert run.phases["survey"]["attempts"] == 2
    assert queue_stub.jobs[-1] == ("run_voyage", (run.phases["survey"]["voyage_id"],), {})

    # 任务还在跑时不接受重试
    busy = await client.post(f"/api/research/{run_id}/retry", headers=headers)
    assert busy.status_code == 409 and busy.json()["detail"] == "RESEARCH_PHASE_RUNNING"


async def test_cancelling_the_task_itself_stops_the_research(client, queue_stub, literature):
    """用户在任务页直接取消了那条任务：对账后研究也是已停止。"""
    headers = await _login(client)
    run_id = (
        await client.post("/api/research", json={"direction": DIRECTION}, headers=headers)
    ).json()["id"]
    run = await _advance(run_id, queue_stub)
    resp = await client.post(
        f"/api/voyages/{run.phases['survey']['voyage_id']}/cancel", headers=headers
    )
    assert resp.status_code == 200
    await worker_tasks.reconcile_research_runs(_ctx(queue_stub))
    assert (await _run_row(run_id)).status == "cancelled"


# ---- 检索失败的几种样子：都不会变成「完成」 ----


async def test_empty_survey_needs_attention_and_edited_scope_restarts_it(
    client, queue_stub, literature
):
    literature["feed"] = _feed([])  # 来源能连上，但什么都没搜到
    headers = await _login(client)
    run_id = (
        await client.post("/api/research", json={"direction": DIRECTION}, headers=headers)
    ).json()["id"]
    run = await _advance(run_id, queue_stub)
    first_survey = run.phases["survey"]["voyage_id"]
    await worker_tasks.run_voyage(_ctx(queue_stub), first_survey)

    detail = (await client.get(f"/api/research/{run_id}", headers=headers)).json()
    assert detail["phase"] == "survey" and detail["status"] == "needs_attention"
    assert detail["attention"]["code"] == "SURVEY_NO_RESULTS"
    assert detail["attention"]["action"] == "edit_scope"
    assert detail["survey"]["state"] == "done"  # 任务本身是「完成」，研究不是
    assert detail["survey"]["notes"]  # 检索的提示原样给界面
    assert detail["hypotheses"]["voyage_id"] is None

    # 改了关键词再来：简报和库的收录设置都更新，新建一条检索任务
    literature["feed"] = _feed(SIX_RELEVANT)
    resp = await client.post(
        f"/api/research/{run_id}/retry",
        json={"brief": {"include_keywords": ["agent workflow verification"]}},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    run = await _run_row(run_id)
    assert run.status == "running" and run.attention is None
    assert run.brief["include_keywords"] == ["agent workflow verification"]
    second_survey = run.phases["survey"]["voyage_id"]
    assert second_survey != first_survey
    async with get_sessionmaker()() as session:
        library = await session.get(DirectionLibrary, run.library_id)
        assert library.definition["keywords"]["include"] == ["agent workflow verification"]
        voyage = await session.get(VoyageRun, uuid.UUID(second_survey))
        assert voyage.checkpoint["params"]["query_terms"] == ["agent workflow verification"]
    await worker_tasks.run_voyage(_ctx(queue_stub), second_survey)
    assert (await _run_row(run_id)).phase == "hypotheses"


async def test_too_few_relevant_then_retry_broadens(client, queue_stub, literature):
    literature["feed"] = _feed(SIX_RELEVANT[:2] + SIX_RELEVANT[-1:])  # 只有 2 篇相关
    headers = await _login(client)
    run_id = (
        await client.post("/api/research", json={"direction": DIRECTION}, headers=headers)
    ).json()["id"]
    run = await _advance(run_id, queue_stub)
    await worker_tasks.run_voyage(_ctx(queue_stub), run.phases["survey"]["voyage_id"])
    run = await _run_row(run_id)
    assert run.status == "needs_attention"
    assert run.attention["code"] == "SURVEY_TOO_FEW_RELEVANT"
    assert run.attention["action"] == "broaden"
    assert run.attention["counts"] == {"found": 3, "relevant": 2}  # 检索到 3 篇，留下 2 篇

    # 界面发 {broaden: true}（不带参数的重试在这种情况下也一样）：门槛 0.35、回溯 60 个月
    resp = await client.post(
        f"/api/research/{run_id}/retry", json={"broaden": True}, headers=headers
    )
    assert resp.status_code == 200
    run = await _run_row(run_id)
    assert run.phases["survey"]["broadened"] is True
    async with get_sessionmaker()() as session:
        voyage = await session.get(VoyageRun, uuid.UUID(run.phases["survey"]["voyage_id"]))
        knobs = voyage.checkpoint["params"]["knobs"]
    assert knobs["relevance_threshold"] == 0.35 and knobs["months_back"] == 60


async def test_failed_survey_step_needs_attention_and_retry_resumes_it(
    client, queue_stub, literature, monkeypatch
):
    """检索任务停在出错状态（不是临时故障，不会自动重试）→ SURVEY_FAILED；
    重试是从断点接着跑同一条任务，不是新建。"""
    headers = await _login(client)
    run_id = (
        await client.post("/api/research", json={"direction": DIRECTION}, headers=headers)
    ).json()["id"]
    run = await _advance(run_id, queue_stub)
    survey_id = run.phases["survey"]["voyage_id"]

    from app.agents.voyage import actions as actions_registry

    real = actions_registry.get_action("wiki.compile")

    async def _boom(ctx, params):
        raise ValueError("compile exploded")

    monkeypatch.setitem(actions_registry._REGISTRY, "wiki.compile", _boom)  # noqa: SLF001
    await worker_tasks.run_voyage(_ctx(queue_stub), survey_id)
    run = await _run_row(run_id)
    assert run.status == "needs_attention" and run.attention["code"] == "SURVEY_FAILED"
    assert run.attention["detail"]  # 带着那一步的一句话原因
    detail = (await client.get(f"/api/research/{run_id}", headers=headers)).json()
    assert detail["survey"]["state"] == "paused_error"  # 任务的状态原样给出
    assert detail["survey"]["step_title"]  # 停在哪一步
    assert detail["last_error"]["code"] and detail["last_error"]["transient"] is False
    assert detail["next_retry_at"] is None and detail["retry_attempt"] == 0

    monkeypatch.setitem(actions_registry._REGISTRY, "wiki.compile", real)  # noqa: SLF001
    resp = await client.post(f"/api/research/{run_id}/retry", headers=headers)
    assert resp.status_code == 200 and resp.json()["status"] == "running"
    assert queue_stub.jobs[-1] == ("resume_voyage", (survey_id,), {})
    run = await _run_row(run_id)
    assert run.phases["survey"]["voyage_id"] == survey_id  # 同一条任务
    await worker_tasks.resume_voyage(_ctx(queue_stub), survey_id)
    assert (await _run_row(run_id)).phase == "hypotheses"


async def test_transient_failure_with_a_pending_auto_retry_still_counts_as_running(
    client, queue_stub, literature, monkeypatch
):
    headers = await _login(client)
    run_id = (
        await client.post("/api/research", json={"direction": DIRECTION}, headers=headers)
    ).json()["id"]
    run = await _advance(run_id, queue_stub)
    survey_id = run.phases["survey"]["voyage_id"]

    from app.agents.voyage import actions as actions_registry

    async def _down(ctx, params):
        raise httpx.ConnectError("no route to host")

    monkeypatch.setitem(actions_registry._REGISTRY, "wiki.search_candidates", _down)  # noqa: SLF001
    await worker_tasks.run_voyage(_ctx(queue_stub), survey_id)
    async with get_sessionmaker()() as session:
        voyage = await session.get(VoyageRun, uuid.UUID(survey_id))
        assert voyage.status == "paused_error"
        assert voyage.checkpoint["auto_retry"]["next_at"]
    run = await _run_row(run_id)
    assert run.status == "running" and run.attention is None
    detail = (await client.get(f"/api/research/{run_id}", headers=headers)).json()
    assert detail["survey"]["state"] == "paused_error" and detail["next_retry_at"]
    assert detail["retry_attempt"] == 1
    assert detail["last_error"]["transient"] is True
    assert set(detail["last_error"]) == {"code", "message", "transient", "source"}

    # 自动重试用完：来源连不上
    async with get_sessionmaker()() as session:
        voyage = await session.get(VoyageRun, uuid.UUID(survey_id))
        checkpoint = dict(voyage.checkpoint)
        checkpoint["auto_retry"] = {"attempt": 3, "next_at": None}
        voyage.checkpoint = checkpoint
        await session.commit()
    await worker_tasks.reconcile_research_runs(_ctx(queue_stub))
    run = await _run_row(run_id)
    assert run.status == "needs_attention"
    assert run.attention["code"] == "SURVEY_SOURCES_UNREACHABLE"


# ---- arXiv 失败不再拖垮整次检索 ----


class _DeadArxiv:
    """连不上的 arXiv 客户端（调研检索用 search_relevance）。"""

    page_size = 100

    async def search_relevance(self, *_args, **_kwargs):
        raise httpx.ConnectError("arxiv is unreachable")


def _oa_row(i: int) -> dict:
    return {
        "openalex_id": f"https://openalex.org/W{i}",
        "title": f"OpenAlex Agent Workflow Paper {i}",
        "doi": f"10.1000/oa.{i}",
        "authors": [{"name": "A"}],
        "abstract": "agent verification workflow",
        "year": 2026,
        "published": "2026-03-01",
        "venue": "OA",
        "url": f"https://example.org/{i}",
        "cited_by_count": 10 * i,
        "referenced_works": [],
    }


class _OpenAlexStub:
    """OpenAlex 客户端的替身：每条查询都回同一批；也能整个连不上。"""

    def __init__(self, rows=None, error=None):
        self.rows, self.error = list(rows or []), error
        self.queries: list[str] = []

    async def search_works(self, query, *, limit=5, **_kwargs):
        self.queries.append(query)
        if self.error is not None:
            raise self.error
        return self.rows[:limit]

    async def find_work(self, **_kwargs):
        return None

    async def works_by_ids(self, _ids):
        return []

    async def citing_works(self, _id, **_kwargs):
        return []


async def _search_step(monkeypatch, queue_stub, client, *, openalex) -> tuple[str, VoyageRun]:
    """建一次研究（来源 arXiv + OpenAlex，arXiv 连不上），只跑检索那一步。"""
    monkeypatch.setattr(actions_wiki, "_configured_sources", lambda _kw: ["arxiv", "openalex"])
    monkeypatch.setattr(actions_wiki, "get_arxiv_client", lambda: _DeadArxiv())
    monkeypatch.setattr(actions_wiki, "get_openalex_client", lambda: openalex)
    headers = await _login(client)
    run_id = (
        await client.post("/api/research", json={"direction": DIRECTION}, headers=headers)
    ).json()["id"]
    run = await _advance(run_id, queue_stub)
    survey_id = uuid.UUID(run.phases["survey"]["voyage_id"])
    await _engine().run(survey_id)
    async with get_sessionmaker()() as session:
        return run_id, await session.get(VoyageRun, survey_id)


async def test_arxiv_failure_is_soft_when_another_source_returned_results(
    client, queue_stub, literature, monkeypatch
):
    openalex = _OpenAlexStub(rows=[_oa_row(i) for i in range(6)])
    _run_id, voyage = await _search_step(monkeypatch, queue_stub, client, openalex=openalex)
    # 每条查询各查一次（拼成一句长查询的话，这些来源几乎什么都搜不到）；
    # 前几条再在回溯窗口里查一遍，保证有近期的
    keywords = ["agent planning", "verification workflow", "retrieval augmented generation"]
    assert openalex.queries == keywords + keywords
    stats = voyage.checkpoint["ingest_search_stats"]
    assert stats["strategy"] == "survey"
    assert stats["selected"] == 6 and stats["inserted"] == 6  # 六条查询回的是同样六篇
    assert stats["source_fetched"] == 36 and stats["source_counts"] == {"openalex": 36}
    assert stats["source_selected"] == {"openalex": 6}
    assert [e["source"] for e in stats["source_errors"]] == ["arxiv"]
    assert "ConnectError" in stats["source_errors"][0]["error"]
    assert any("arxiv" in m for m in stats["messages"])
    # 每一次请求都留了记录：来源、查询、排序、时间窗、回了几条
    logged = [q for q in stats["queries"] if q["source"] == "openalex"]
    assert [q["query"] for q in logged] == keywords + keywords
    assert all(q["sort"] == "relevance" and q["returned"] == 6 for q in logged)
    assert logged[0]["window"].startswith("2016-") and logged[3]["window"].startswith("2023-")
    assert [q for q in stats["queries"] if q["source"] == "arxiv"][0]["error"]
    async with get_sessionmaker()() as session:
        first = (
            await session.execute(
                select(VoyageStep).where(VoyageStep.run_id == voyage.id).order_by(VoyageStep.seq)
            )
        ).scalars().first()
    assert first.action == "wiki.search_candidates" and first.status == "passed"
    assert first.observation["queries"] == stats["queries"]


async def test_search_fails_the_step_when_every_source_is_unreachable(
    client, queue_stub, literature, monkeypatch
):
    """每个来源都连不上：这一步失败（临时故障 → 排自动重试），不是带着零篇「完成」。"""
    run_id, voyage = await _search_step(
        monkeypatch, queue_stub, client, openalex=_OpenAlexStub(error=httpx.ReadTimeout("slow"))
    )
    assert voyage.status == "paused_error"
    error = voyage.checkpoint["last_error"]
    assert error["transient"] is True and error["code"] == "NETWORK"
    assert voyage.checkpoint["auto_retry"]["next_at"]
    assert "ingest_search_stats" not in voyage.checkpoint
    run = await _advance(run_id, queue_stub)
    assert run.status == "running"  # 还在等自动重试


# ---- 手动节奏的库不进每日自动同步 ----


async def test_manual_cadence_libraries_are_skipped_by_the_daily_sync(client, queue_stub):
    from tests.conftest import make_project_with_library

    headers = await _login(client)
    _pid, manual_id = await make_project_with_library(client, headers, name="manual-lib")
    _pid2, daily_id = await make_project_with_library(client, headers, name="daily-lib")
    async with get_sessionmaker()() as session:
        manual = await session.get(DirectionLibrary, manual_id)
        daily = await session.get(DirectionLibrary, daily_id)
        for library in (manual, daily):
            library.ingest_state = {"watermark": utcnow().isoformat()}
        manual.cadence = "manual"
        manual.definition = {**(manual.definition or {}), "cadence": "manual"}
        await session.commit()
        due = {lib.id for lib in await ingest_service.find_due_daily_libraries(session)}
    assert daily_id in due and manual_id not in due


def test_research_tasks_are_registered():
    from app.core.queue import WORKER_FUNCTIONS
    from app.core.scheduler import default_jobs

    assert "research_advance" in WORKER_FUNCTIONS
    jobs = {job.name: job for job in default_jobs()}
    assert jobs["reconcile_research_runs"].every_s == 60
    assert isinstance(StubQueue(), StubQueue)


async def test_delete_keeps_the_topic_unless_purged(client, queue_stub, literature):
    headers = await _login(client)
    ids = []
    for text in ("方向甲", "方向乙"):
        run_id = (
            await client.post("/api/research", json={"direction": text}, headers=headers)
        ).json()["id"]
        await _advance(run_id, queue_stub)
        ids.append(run_id)
    keep, purge = [await _run_row(i) for i in ids]

    resp = await client.delete(f"/api/research/{keep.id}", headers=headers)
    assert resp.status_code == 204
    resp = await client.delete(f"/api/research/{purge.id}?purge=true", headers=headers)
    assert resp.status_code == 204
    assert (await client.get("/api/research", headers=headers)).json() == []
    async with get_sessionmaker()() as session:
        # 只删记录的那次：课题和库都在，没结束的检索任务被取消
        assert await session.get(Project, keep.project_id) is not None
        assert await session.get(DirectionLibrary, keep.library_id) is not None
        voyage = await session.get(VoyageRun, uuid.UUID(keep.phases["survey"]["voyage_id"]))
        assert voyage.status == "cancelled"
        # 一并删除的那次：课题和库都没了
        assert await session.get(Project, purge.project_id) is None
        assert await session.get(DirectionLibrary, purge.library_id) is None


async def test_retry_restores_a_run_stopped_while_waiting_for_picks(client, queue_stub):
    headers = await _login(client)
    run_id = (
        await client.post("/api/research", json={"direction": DIRECTION}, headers=headers)
    ).json()["id"]
    async with get_sessionmaker()() as session:
        row = await session.get(ResearchRun, uuid.UUID(run_id))
        row.phase, row.status = "choose", "cancelled"
        await session.commit()
    resp = await client.post(f"/api/research/{run_id}/retry", headers=headers)
    assert resp.status_code == 200 and resp.json()["status"] == "waiting_user"
    again = await client.post(f"/api/research/{run_id}/retry", headers=headers)
    assert again.status_code == 409 and again.json()["detail"] == "RESEARCH_NOTHING_TO_RETRY"


async def test_survey_does_not_finish_when_no_paper_could_be_screened(
    client, queue_stub, literature, monkeypatch
):
    """模型连不上时每篇论文的打分都失败：检索任务不能带着「一篇没筛」显示完成
    （真实运行里撞到过：网关断开，任务一路完成，研究停在「相关的太少」）。
    现在这一步失败并排自动重试，研究仍是运行中；模型恢复后重试照常跑完。"""
    from app.core.llm.fake import FakeProvider

    headers = await _login(client)
    run_id = (
        await client.post("/api/research", json={"direction": DIRECTION}, headers=headers)
    ).json()["id"]
    run = await _advance(run_id, queue_stub)
    survey_id = run.phases["survey"]["voyage_id"]

    real = FakeProvider._respond_relevance

    def _down(last_user: str) -> str:
        raise LLMOverloadedError("gateway closed the connection")

    monkeypatch.setattr(FakeProvider, "_respond_relevance", staticmethod(_down))
    await worker_tasks.run_voyage(_ctx(queue_stub), survey_id)
    async with get_sessionmaker()() as session:
        voyage = await session.get(VoyageRun, uuid.UUID(survey_id))
        assert voyage.status == "paused_error"
        assert voyage.checkpoint["last_error"]["transient"] is True
        assert voyage.checkpoint["auto_retry"]["next_at"]
    detail = (await client.get(f"/api/research/{run_id}", headers=headers)).json()
    assert detail["status"] == "running" and detail["attention"] is None
    assert detail["survey"]["progress"]["found"] == 7
    assert detail["survey"]["progress"]["screened"] == 0
    assert detail["survey"]["step_title"] == "相关性打分（LLM）"
    # 动作名：界面按它出自己的说法（标题是写给日志的，只有中文）
    assert detail["survey"]["step_action"] == "wiki.score_relevance"

    monkeypatch.setattr(FakeProvider, "_respond_relevance", staticmethod(real))
    await worker_tasks.resume_voyage(_ctx(queue_stub), survey_id)
    assert (await _run_row(run_id)).phase == "hypotheses"
