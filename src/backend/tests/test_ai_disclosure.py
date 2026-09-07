"""AI 使用披露声明生成器（#691）：facts 聚合确定性 + 三模板×两语言渲染 + 端点权限。

facts 全部由既有留痕（llm_usage / voyage_runs / manuscript_file_versions）确定性
拼出，测试据此断言：同一份数据两次聚合逐字节一致；取不到的面（模型名、编辑记录）
如实标注而不是编造。
"""

import uuid

import pytest

from app.core.db import get_sessionmaker
from app.models.llm_config import LLMUsage
from app.models.manuscript import Manuscript, ManuscriptFile, ManuscriptFileVersion
from app.models.voyage import VoyageRun
from app.services import ai_disclosure
from tests.conftest import register_and_login

pytestmark = pytest.mark.asyncio


async def _setup_project(client, email="disclose@example.com"):
    token = await register_and_login(client, email)
    headers = {"Authorization": f"Bearer {token}"}
    resp = await client.post("/api/projects", json={"name": "disclosure-proj"}, headers=headers)
    assert resp.status_code == 201
    return resp.json()["id"], headers


async def _seed_manuscript(project_id: str, title="Graph Retrieval") -> uuid.UUID:
    async with get_sessionmaker()() as session:
        ms = Manuscript(project_id=uuid.UUID(project_id), title=title, template="neurips2026")
        session.add(ms)
        await session.commit()
        return ms.id


async def _seed_run(
    project_id: str,
    *,
    kind: str,
    manuscript_id: uuid.UUID | None = None,
    usage: dict | None = None,
    status: str = "done",
) -> uuid.UUID:
    checkpoint = (
        {"params": {"manuscript_id": str(manuscript_id)}} if manuscript_id else {"params": {}}
    )
    async with get_sessionmaker()() as session:
        run = VoyageRun(
            kind=kind,
            status=status,
            goal=f"{kind} run",
            project_id=uuid.UUID(project_id),
            checkpoint=checkpoint,
            usage=usage,
        )
        session.add(run)
        await session.commit()
        return run.id


async def _seed_usage(voyage_id: uuid.UUID, rows: list[tuple[str, str, int, int]]) -> None:
    """rows: (stage, model, prompt_tokens, completion_tokens)。"""
    async with get_sessionmaker()() as session:
        for stage, model, prompt, completion in rows:
            session.add(
                LLMUsage(
                    voyage_id=voyage_id,
                    stage=stage,
                    model=model,
                    prompt_tokens=prompt,
                    completion_tokens=completion,
                )
            )
        await session.commit()


async def _seed_versions(manuscript_id: uuid.UUID, origins: list[str]) -> None:
    async with get_sessionmaker()() as session:
        file = ManuscriptFile(
            manuscript_id=manuscript_id, path="main.tex", content="\\section{Intro}"
        )
        session.add(file)
        await session.flush()
        for seq, origin in enumerate(origins, start=1):
            session.add(
                ManuscriptFileVersion(
                    file_id=file.id, seq=seq, origin=origin, content=f"v{seq}"
                )
            )
        await session.commit()


# ---- facts 聚合 ----


async def test_facts_manuscript_with_runs_and_versions(client):
    """discovery + 写作 run + 版本记录齐备的稿件：全溯源面都能取到，两次聚合一致。"""
    project_id, _ = await _setup_project(client)
    ms_id = await _seed_manuscript(project_id)
    writing_run = await _seed_run(project_id, kind="paper_writing", manuscript_id=ms_id)
    # 同课题另有一个 discovery run（不指向该稿件）：不得混进稿件披露
    stray = await _seed_run(project_id, kind="discovery")
    await _seed_usage(
        writing_run,
        [("writing", "fake-writer", 100, 40), ("writing", "fake-writer", 50, 10)],
    )
    await _seed_usage(stray, [("hyp_generate", "fake-hyp", 10, 5)])
    await _seed_versions(ms_id, ["pre_ai", "pre_ai", "compile"])

    async with get_sessionmaker()() as session:
        facts = await ai_disclosure.build_disclosure_facts(session, manuscript_id=ms_id)
        again = await ai_disclosure.build_disclosure_facts(session, manuscript_id=ms_id)
    assert facts == again  # 确定性：同一数据两次聚合逐项一致
    assert facts["subject"]["type"] == "manuscript"
    assert facts["ai_used"] is True
    assert [r["run_id"] for r in facts["runs"]] == [str(writing_run)]
    run = facts["runs"][0]
    assert run["kind"] == "paper_writing"
    assert run["outputs"] == ["manuscript_text"]
    # 同 stage+model 聚成一行，token/调用数求和
    assert run["stages"] == [
        {
            "stage": "writing",
            "model": "fake-writer",
            "calls": 2,
            "prompt_tokens": 150,
            "completion_tokens": 50,
        }
    ]
    assert facts["models"] == ["fake-writer"]
    assert facts["totals"] == {"prompt_tokens": 150, "completion_tokens": 50, "calls": 2}
    assert facts["editing"] == {
        "ai_write_snapshots": 2,
        "files_with_ai_writes": 1,
        "compile_snapshots": 1,
        "restore_snapshots": 0,
    }
    assert "human_edits_not_itemized" in facts["notes"]


async def test_facts_discovery_run(client):
    project_id, _ = await _setup_project(client, email="disclose2@example.com")
    run_id = await _seed_run(project_id, kind="discovery")
    await _seed_usage(
        run_id,
        [
            ("hyp_generate", "fake-mid", 200, 80),
            ("hyp_compare", "fake-short", 30, 10),
            ("discovery_plan", "fake-mid", 50, 20),
        ],
    )
    async with get_sessionmaker()() as session:
        facts = await ai_disclosure.build_disclosure_facts(session, run_id=run_id)
    assert facts["subject"]["type"] == "voyage"
    assert facts["runs"][0]["outputs"] == ["hypothesis_tree", "research_proposal"]
    # stage 行按 (stage, model) 排序，输出稳定
    assert [r["stage"] for r in facts["runs"][0]["stages"]] == [
        "discovery_plan",
        "hyp_compare",
        "hyp_generate",
    ]
    assert facts["models"] == ["fake-mid", "fake-short"]
    assert facts["stages"] == ["discovery_plan", "hyp_compare", "hyp_generate"]


async def test_facts_manuscript_without_ai(client):
    """无 run、无版本记录：如实说明未使用，而不是硬凑一个披露。"""
    project_id, _ = await _setup_project(client, email="disclose3@example.com")
    ms_id = await _seed_manuscript(project_id)
    async with get_sessionmaker()() as session:
        facts = await ai_disclosure.build_disclosure_facts(session, manuscript_id=ms_id)
    assert facts["ai_used"] is False
    assert facts["runs"] == []
    assert facts["editing"] is None
    assert "no_runs_recorded" in facts["notes"]
    assert "no_edit_history" in facts["notes"]

    zh = ai_disclosure.render_disclosure(facts, "generic", "zh")
    assert "未使用 AI 工具" in zh["statement"]
    en = ai_disclosure.render_disclosure(facts, "icmje", "en")
    assert "no artificial intelligence (AI)-assisted technologies were used" in en["statement"]


async def test_facts_model_unrecorded(client):
    """记账明细缺失但 run 累计用量非零：模型名如实标 unknown，不编造。"""
    project_id, _ = await _setup_project(client, email="disclose4@example.com")
    run_id = await _seed_run(
        project_id,
        kind="discovery",
        usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    )
    async with get_sessionmaker()() as session:
        facts = await ai_disclosure.build_disclosure_facts(session, run_id=run_id)
    assert facts["ai_used"] is True
    assert facts["models"] == []
    assert facts["runs"][0]["stages"][0]["model"] == "unknown"
    assert f"model_unrecorded:{run_id}" in facts["notes"]
    rendered = ai_disclosure.render_disclosure(facts, "generic", "en")
    assert "an AI model whose name was not recorded" in rendered["statement"]
    rendered = ai_disclosure.render_disclosure(facts, "generic", "zh")
    assert "未记录到具体模型名" in rendered["statement"]


# ---- 三模板 × 两语言渲染 ----


def _sample_facts() -> dict:
    return {
        "version": 1,
        "subject": {"type": "manuscript", "id": "x", "title": "T"},
        "ai_used": True,
        "runs": [
            {
                "run_id": "r1",
                "kind": "paper_writing",
                "status": "done",
                "goal": "写稿",
                "outputs": ["manuscript_text"],
                "stages": [
                    {
                        "stage": "writing",
                        "model": "fake-writer",
                        "calls": 3,
                        "prompt_tokens": 100,
                        "completion_tokens": 30,
                    }
                ],
            }
        ],
        "models": ["fake-writer"],
        "stages": ["writing"],
        "totals": {"prompt_tokens": 100, "completion_tokens": 30, "calls": 3},
        "editing": None,
        "notes": [],
    }


async def test_render_all_styles_and_langs():
    """快照式关键句断言：三家模板的标志性措辞 + 模型名 + 附录明细齐备。"""
    facts = _sample_facts()
    key_sentences = {
        ("icmje", "en"): [
            "ICMJE recommendations",
            "The following tools were used: fake-writer",
            "take full responsibility",
            "No AI tool is listed as an author",
        ],
        ("icmje", "zh"): ["ICMJE 建议", "fake-writer", "负全部责任", "未将任何 AI 工具列为作者"],
        ("elsevier", "en"): [
            "Declaration of generative AI and AI-assisted technologies",
            "During the preparation of this work the author(s) used fake-writer",
            "reviewed and edited",
            "before the references",
        ],
        ("elsevier", "zh"): ["生成式 AI 与 AI 辅助技术的声明", "fake-writer", "参考文献之前"],
        ("generic", "en"): ["AI Use Disclosure", "fake-writer", "full responsibility"],
        ("generic", "zh"): ["AI 使用披露", "fake-writer", "负全部责任"],
    }
    for (style, lang), expected in key_sentences.items():
        out = ai_disclosure.render_disclosure(facts, style, lang)
        for sentence in expected:
            assert sentence in out["statement"], (style, lang, sentence)
        # 附录：明细表含 run 与模型
        assert "r1" in out["appendix"]
        assert "fake-writer" in out["appendix"]
        assert out["appendix"].startswith("## ")


async def test_render_rejects_unknown_style_and_lang():
    facts = _sample_facts()
    with pytest.raises(ValueError):
        ai_disclosure.render_disclosure(facts, "nature", "en")
    with pytest.raises(ValueError):
        ai_disclosure.render_disclosure(facts, "icmje", "fr")


# ---- 端点与权限 ----


async def test_manuscript_disclosure_endpoint(client):
    project_id, headers = await _setup_project(client, email="disclose5@example.com")
    ms_id = await _seed_manuscript(project_id)
    run_id = await _seed_run(project_id, kind="paper_writing", manuscript_id=ms_id)
    await _seed_usage(run_id, [("writing", "fake-writer", 10, 5)])

    resp = await client.get(
        f"/api/manuscripts/{ms_id}/ai-disclosure?style=elsevier&lang=en", headers=headers
    )
    assert resp.status_code == 200
    data = resp.json()
    assert set(data) == {"statement", "appendix", "facts"}
    assert "fake-writer" in data["statement"]
    assert data["facts"]["ai_used"] is True

    # 非法 style 被 422 顶回（Literal 校验）
    resp = await client.get(
        f"/api/manuscripts/{ms_id}/ai-disclosure?style=nature", headers=headers
    )
    assert resp.status_code == 422

    # 非课题主人 404（与稿件详情同口径，不泄露存在性）
    other = await register_and_login(client, "disclose5b@example.com")
    resp = await client.get(
        f"/api/manuscripts/{ms_id}/ai-disclosure",
        headers={"Authorization": f"Bearer {other}"},
    )
    assert resp.status_code == 404


async def test_voyage_disclosure_endpoint(client):
    project_id, headers = await _setup_project(client, email="disclose6@example.com")
    run_id = await _seed_run(project_id, kind="discovery")
    await _seed_usage(run_id, [("hyp_generate", "fake-mid", 20, 8)])

    resp = await client.get(
        f"/api/voyages/{run_id}/ai-disclosure?style=icmje&lang=zh", headers=headers
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "ICMJE 建议" in data["statement"]
    assert data["facts"]["subject"]["type"] == "voyage"

    other = await register_and_login(client, "disclose6b@example.com")
    resp = await client.get(
        f"/api/voyages/{run_id}/ai-disclosure",
        headers={"Authorization": f"Bearer {other}"},
    )
    assert resp.status_code == 404
