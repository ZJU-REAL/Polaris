"""M5-B 写作 voyage 测试（fake LLM + 假 tectonic，直接驱动 VoyageEngine）：

- draft 接口：kind=paper_writing、同 manuscript 互斥 409、非法节名 422；
- 全链路：分节固定顺序 → 中期编译 → Related Work 延后（候选集 = 库内 + S2 命中，
  被引 S2 追加进 fact_pack）→ 终编译 ok → done，manuscript draft→writing→compiled；
- 静态校验拒绝路径（fake 标记 INVALID_CITE_TEST）：重写用尽 → voyage failed、
  稿件回退 draft；
- validate_section_text 三类违规与白名单豁免单元测试。
"""

import uuid
from pathlib import Path

import pytest_asyncio

from app.agents.voyage import VoyageEngine, actions_writing
from app.agents.voyage.actions_writing import validate_section_text
from app.core.llm.router import LLMRouter
from app.services import latex_compile
from app.services.latex_compile import TectonicRun
from tests.conftest import RecordingBus
from tests.test_manuscripts import (
    _create_manuscript,
    _seed_experiment,
    _seed_idea,
    _seed_paper,
    _setup_project,
)

FACT_PACK = {
    "citations": [
        {"bibkey": "smith2017attention", "title": "Attention", "year": 2017},
        {"bibkey": "lee2023graph", "title": "Graphs", "year": 2023},
    ],
    "figures": [{"fig_id": "exp_fig_0", "caption": "c", "source": "experiment"}],
    "metrics": [
        {
            "name": "accuracy",
            "runs": [{"seq": 1, "value": 0.7}, {"seq": 2, "value": 0.8}],
            "best": 0.8,
        }
    ],
}


@pytest_asyncio.fixture(autouse=True)
async def _clean_crdt():
    from app.services.crdt_rooms import reset_crdt_rooms

    yield
    await reset_crdt_rooms()


@pytest_asyncio.fixture(autouse=True)
def _stub_tectonic(monkeypatch):
    """假 tectonic：直接产出 main.pdf（终编译 ok 路径）。"""

    def ok_run(binary: str, workdir: Path) -> TectonicRun:
        (workdir / "main.pdf").write_bytes(b"%PDF stub")
        (workdir / "main.log").write_text("", encoding="utf-8")
        return TectonicRun(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(latex_compile, "_find_tectonic", lambda: "/usr/bin/tectonic")
    monkeypatch.setattr(latex_compile, "_run_tectonic", ok_run)


@pytest_asyncio.fixture(autouse=True)
def _stub_s2(monkeypatch):
    """S2 title 检索桩：返回一条不在库内的命中（Related Work 候选集）。"""

    async def fake_s2(title: str):
        return [
            {
                "title": "A Related S2 Paper",
                "year": 2024,
                "authors": ["Jane Doe"],
                "venue": "arXiv",
                "url": "https://example.org/abs/1",
                "source": "s2",
            }
        ]

    monkeypatch.setattr(actions_writing, "_s2_candidates", fake_s2)


def _make_engine() -> tuple[VoyageEngine, RecordingBus]:
    bus = RecordingBus()
    return VoyageEngine(event_bus=bus, llm_router=LLMRouter()), bus


async def _prepare(client, queue_stub, **draft_body):
    project_id, headers = await _setup_project(client)
    idea_id = await _seed_idea(project_id)
    exp_id = await _seed_experiment(project_id, idea_id)
    await _seed_paper(project_id, "Attention Is All You Need", 2017)
    resp = await _create_manuscript(
        client, headers, project_id, idea_id=idea_id, experiment_id=exp_id
    )
    ms_id = resp.json()["id"]
    resp = await client.post(
        f"/api/manuscripts/{ms_id}/draft", json=draft_body or None, headers=headers
    )
    assert resp.status_code == 201, resp.text
    return project_id, headers, ms_id, resp.json()


async def test_draft_endpoint_and_conflict(client, queue_stub):
    _, headers, ms_id, voyage = await _prepare(client, queue_stub)
    assert voyage["kind"] == "paper_writing"
    assert ("run_voyage", (voyage["id"],), {}) in queue_stub.jobs

    detail = (await client.get(f"/api/manuscripts/{ms_id}", headers=headers)).json()
    assert detail["writing_voyage_id"] == voyage["id"]

    # 同 manuscript 互斥 409
    resp = await client.post(f"/api/manuscripts/{ms_id}/draft", json={}, headers=headers)
    assert resp.status_code == 409 and resp.json()["detail"] == "WRITING_IN_PROGRESS"

    # 非法节名 422
    resp = await client.post(f"/api/voyages/{voyage['id']}/cancel", headers=headers)
    assert resp.status_code == 200
    resp = await client.post(
        f"/api/manuscripts/{ms_id}/draft", json={"sections": ["acknowledgements"]}, headers=headers
    )
    assert resp.status_code == 422 and resp.json()["detail"] == "INVALID_SECTIONS"


async def test_writing_full_pipeline(client, queue_stub):
    _, headers, ms_id, voyage = await _prepare(client, queue_stub)
    engine, bus = _make_engine()
    await engine.run(uuid.UUID(voyage["id"]))

    resp = await client.get(f"/api/voyages/{voyage['id']}", headers=headers)
    run = resp.json()
    assert run["status"] == "done", run
    actions = [s["action"] for s in run["steps"]]
    # 分节固定顺序 → 中期编译 → Related Work 延后 → 终编译
    assert actions == [
        "writing.section",
        "writing.section",
        "writing.section",
        "writing.section",
        "writing.section",
        "writing.section",
        "writing.compile",
        "writing.related_work",
        "writing.compile",
    ]
    sections = [s["params"]["section"] for s in run["steps"] if s["action"] == "writing.section"]
    assert sections == [
        "introduction",
        "method",
        "experimental_setup",
        "results",
        "conclusion",
        "abstract",
    ]
    assert all(s["status"] == "passed" for s in run["steps"])
    assert run["steps"][6]["observation"]["phase"] == "mid"
    related_obs = run["steps"][7]["observation"]
    assert related_obs["candidates"] == 2  # 库内 1 + S2 命中 1
    assert related_obs["s2_cited_added"] == 1  # fake 引了 s2 候选 → 追加 fact_pack
    assert related_obs["via_room"] is False  # 无活跃 CRDT 房间：直写库
    final_obs = run["steps"][8]["observation"]
    assert final_obs["phase"] == "final" and final_obs["status"] == "ok"

    # 稿件：状态 compiled、节内容写入 main.tex 对应标记区间、S2 引用入 fact_pack
    detail = (await client.get(f"/api/manuscripts/{ms_id}", headers=headers)).json()
    assert detail["status"] == "compiled"
    assert detail["latest_compile"]["status"] == "ok"
    main_id = next(f["id"] for f in detail["files"] if f["path"] == "main.tex")
    content = (
        await client.get(f"/api/manuscripts/{ms_id}/files/{main_id}", headers=headers)
    ).json()["content"]
    assert "% fake introduction draft" in content
    assert "% fake abstract draft" in content
    assert "\\cite{smith2017attention}" in content
    assert "\\includegraphics[width=\\linewidth]{figures/exp_fig_0.pdf}" in content
    assert "（待撰写 / to be drafted）" not in content.split("% POLARIS_SECTION: introduction")[1]
    s2_cites = [c for c in detail["fact_pack"]["citations"] if c.get("source") == "s2"]
    assert [c["bibkey"] for c in s2_cites] == ["doe2024related"]
    assert "\\cite{doe2024related}" in content

    # manuscript.status 事件：writing → compiled
    statuses = [m["status"] for _, m in bus.notify if m.get("type") == "manuscript.status"]
    assert "writing" in statuses and statuses[-1] == "compiled"


async def test_writing_selected_sections_only(client, queue_stub):
    _, headers, ms_id, voyage = await _prepare(
        client, queue_stub, sections=["results", "introduction"]
    )
    engine, _ = _make_engine()
    await engine.run(uuid.UUID(voyage["id"]))
    resp = await client.get(f"/api/voyages/{voyage['id']}", headers=headers)
    run = resp.json()
    assert run["status"] == "done"
    # 只写选定节（固定顺序 introduction→results），不含 related_work / 中期编译
    assert [s["action"] for s in run["steps"]] == [
        "writing.section",
        "writing.section",
        "writing.compile",
    ]
    assert [s["params"].get("section") for s in run["steps"][:2]] == ["introduction", "results"]
    assert run["steps"][2]["params"]["phase"] == "final"


async def test_draft_auto_refreshes_fact_pack_and_keeps_revision_notes(client, queue_stub):
    """起草前自动重建 fact-pack：建稿后新增的文献入包，评审修订说明保留。"""
    from app.core.db import get_sessionmaker
    from app.models.manuscript import Manuscript

    project_id, headers = await _setup_project(client)
    resp = await _create_manuscript(client, headers, project_id)
    ms_id = resp.json()["id"]
    assert (await client.get(f"/api/manuscripts/{ms_id}", headers=headers)).json()["fact_pack"][
        "citations"
    ] == []

    await _seed_paper(project_id, "Late Added Paper", 2025)
    async with get_sessionmaker()() as session:
        ms = await session.get(Manuscript, uuid.UUID(ms_id))
        ms.fact_pack = dict(ms.fact_pack or {}) | {"revision_notes": "改这里"}
        await session.commit()

    resp = await client.post(f"/api/manuscripts/{ms_id}/draft", json={}, headers=headers)
    assert resp.status_code == 201
    pack = (await client.get(f"/api/manuscripts/{ms_id}", headers=headers)).json()["fact_pack"]
    assert {c["bibkey"] for c in pack["citations"]} == {"smith2025late"}
    assert pack["revision_notes"] == "改这里"


async def test_invalid_cite_degrades_with_todo_and_continues(client, queue_stub):
    """重写用尽仍违规 → 不判整单失败：降级写入 + TODO 标注 + needs_review + Activity。"""
    _, headers, ms_id, voyage = await _prepare(
        client, queue_stub, sections=["introduction"], notes="INVALID_CITE_TEST"
    )
    engine, _ = _make_engine()
    await engine.run(uuid.UUID(voyage["id"]))

    resp = await client.get(f"/api/voyages/{voyage['id']}", headers=headers)
    run = resp.json()
    assert run["status"] == "done"
    step = run["steps"][0]
    assert step["status"] == "passed"
    assert step["observation"]["needs_review"] is True
    assert any("nonexistent_fake_key" in v for v in step["observation"]["violations"])

    # 降级稿已写入：TODO 标注在节顶部
    detail = (await client.get(f"/api/manuscripts/{ms_id}", headers=headers)).json()
    assert detail["status"] == "compiled"  # 终编译（假 tectonic）仍走完
    main = next(f for f in detail["files"] if f["path"] == "main.tex")
    content = (
        await client.get(f"/api/manuscripts/{ms_id}/files/{main['id']}", headers=headers)
    ).json()["content"]
    assert "TODO(AI 起草)" in content
    assert "nonexistent_fake_key" in content

    # Activity 提醒人工核对
    from sqlalchemy import select

    from app.core.db import get_sessionmaker
    from app.models.activity import Activity

    async with get_sessionmaker()() as session:
        acts = (
            (
                await session.execute(
                    select(Activity).where(Activity.kind == "manuscript.section_needs_review")
                )
            )
            .scalars()
            .all()
        )
    assert len(acts) == 1
    assert acts[0].payload["section"] == "introduction"


async def test_final_compile_failure_fails_voyage(client, queue_stub, monkeypatch):
    """终编译不 ok → voyage failed（全文编译 ok 为完成条件）。"""

    def bad_run(binary: str, workdir: Path) -> TectonicRun:
        (workdir / "main.log").write_text(
            "! Undefined control sequence.\nl.3 \\broken\n", encoding="utf-8"
        )
        return TectonicRun(returncode=1, stdout="", stderr="")

    monkeypatch.setattr(latex_compile, "_run_tectonic", bad_run)
    _, headers, ms_id, voyage = await _prepare(client, queue_stub, sections=["introduction"])
    engine, _ = _make_engine()
    await engine.run(uuid.UUID(voyage["id"]))
    run = (await client.get(f"/api/voyages/{voyage['id']}", headers=headers)).json()
    assert run["status"] == "failed"
    assert "终编译未通过" in run["steps"][-1]["observation"]["error"]


def test_validate_section_text_three_rules_and_whitelist():
    # 1) 非法 cite
    bad = validate_section_text(r"as \cite{ghost2020}", FACT_PACK)
    assert bad and "非法引用" in bad[0]
    assert validate_section_text(r"as \cite{smith2017attention,lee2023graph}", FACT_PACK) == []
    assert validate_section_text(r"see \citep[e.g.][]{smith2017attention}", FACT_PACK) == []

    # 2) 非法 includegraphics（fig_id 白名单）
    bad = validate_section_text(r"\includegraphics{figures/unknown.pdf}", FACT_PACK)
    assert bad and "非法图表" in bad[0]
    ok = r"\includegraphics[width=0.9\linewidth]{figures/exp_fig_0.pdf}"
    assert validate_section_text(ok, FACT_PACK) == []  # 选项里的 0.9 不误报

    # 3) 数字：metrics ±0.01 命中 / 编造数字拒绝
    assert validate_section_text("accuracy reaches 0.8 overall", FACT_PACK) == []
    assert validate_section_text("accuracy reaches 0.809 overall", FACT_PACK) == []  # 容差内
    bad = validate_section_text("accuracy reaches 0.93 overall", FACT_PACK)
    assert bad and "0.93" in bad[0]
    assert validate_section_text("improves to 80%", FACT_PACK) == []  # 80% ≙ 0.8
    bad = validate_section_text("improves to 93.5%", FACT_PACK)
    assert bad and "93.5%" in bad[0]

    # 白名单豁免：年份 / 小于 10 的整数 / 章节引用
    assert validate_section_text("first proposed in 2017 and revisited in 2023", FACT_PACK) == []
    assert validate_section_text("we run 3 seeds and 5 baselines", FACT_PACK) == []
    assert validate_section_text("as shown in Section 12 and Table 42", FACT_PACK) == []
    bad = validate_section_text("uses 12345 unlabeled samples", FACT_PACK)
    assert bad == []  # 整数（无小数点、无 %）不在受检类里：\d+\.?\d*% 与小数
    bad = validate_section_text("uses 123.45 unlabeled samples", FACT_PACK)
    assert bad and "123.45" in bad[0]


async def test_related_candidates_dedup(client):
    """S2 命中与库内同名去重 + bibkey 冲突加后缀。"""
    from app.agents.voyage.actions_writing import build_related_candidates

    hits = [
        {"title": "Attention", "year": 2017, "authors": ["Bob Smith"], "source": "s2"},
        {
            "title": "attention is all you need",
            "year": 2017,
            "authors": ["Ada Smith"],
            "source": "s2",
        },
    ]
    pack = {
        "citations": [
            {"bibkey": "smith2017attention", "title": "Attention Is All You Need", "year": 2017}
        ]
    }
    candidates = build_related_candidates(pack, hits)
    # 同名（大小写不敏感）去重；不同题命中 key 冲突 → smith2017attentiona
    assert [c["bibkey"] for c in candidates] == ["smith2017attention", "smith2017attentiona"]
