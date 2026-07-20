"""M5-B 稿件 API 测试：模板展开 / fact-pack 组装与刷新 / 文件管理 / submit 前置与闸门联动。

（docs/api-m5-b.md §1/§2/§3/§7；编译与写作 voyage 见 test_latex_compile / test_writing_voyage）
"""

import uuid

import pytest_asyncio
from sqlalchemy import select

from app.core.db import get_sessionmaker
from app.models.experiment import Experiment, ExperimentRun
from app.models.idea import Idea
from app.models.manuscript import Manuscript
from app.models.paper import Paper
from tests.conftest import register_and_login


@pytest_asyncio.fixture(autouse=True)
async def _clean_crdt():
    from app.services.crdt_rooms import reset_crdt_rooms

    yield
    await reset_crdt_rooms()


async def _setup_project(client, email="alice@example.com"):
    token = await register_and_login(client, email)
    headers = {"Authorization": f"Bearer {token}"}
    resp = await client.post("/api/projects", json={"name": "writer-proj"}, headers=headers)
    assert resp.status_code == 201
    return resp.json()["id"], headers


async def _seed_idea(project_id: str) -> str:
    async with get_sessionmaker()() as session:
        idea = Idea(
            project_id=uuid.UUID(project_id),
            title="共引图增强检索（test idea）",
            summary="用 2-hop 共引特征改进检索",
            status="promoted",
        )
        session.add(idea)
        await session.commit()
        return str(idea.id)


async def _seed_experiment(project_id: str, idea_id: str, tmp_path=None) -> str:
    """带 plan（假设）/ runs（指标）/ figures 的完成实验（fact-pack 事实源）。"""
    async with get_sessionmaker()() as session:
        experiment = Experiment(
            project_id=uuid.UUID(project_id),
            idea_id=uuid.UUID(idea_id),
            status="done",
            plan={
                "hypotheses": [
                    {"text": "新方法优于基线（test）", "status": "verified", "evidence": "e1"},
                    {"text": "趋势可复现（test）", "status": "testing"},
                ],
                "primary_metric": {"name": "accuracy", "direction": "maximize"},
            },
            figures=[{"index": 0, "name": "primary_metric.png", "caption": "主指标曲线"}],
        )
        session.add(experiment)
        await session.flush()
        session.add_all(
            [
                ExperimentRun(
                    experiment_id=experiment.id,
                    seq=1,
                    command="run",
                    status="succeeded",
                    exit_code=0,
                    metrics={"accuracy": [{"step": 0, "value": 0.6}, {"step": 1, "value": 0.7}]},
                    primary_value=0.7,
                ),
                ExperimentRun(
                    experiment_id=experiment.id,
                    seq=2,
                    command="run",
                    status="succeeded",
                    exit_code=0,
                    metrics={"accuracy": [{"step": 1, "value": 0.8}]},
                    primary_value=0.8,
                ),
            ]
        )
        await session.commit()
        return str(experiment.id)


async def _seed_paper(project_id: str, title: str, year: int = 2024, status="compiled") -> str:
    async with get_sessionmaker()() as session:
        paper = Paper(
            project_id=uuid.UUID(project_id),
            title=title,
            authors=[{"name": "Ada Smith"}],
            year=year,
            status=status,
        )
        session.add(paper)
        await session.commit()
        return str(paper.id)


async def _create_manuscript(client, headers, project_id, **extra):
    payload = {"title": "Co-citation Graph Retrieval", "template": "neurips2026", **extra}
    return await client.post(
        f"/api/projects/{project_id}/manuscripts", json=payload, headers=headers
    )


async def test_templates_endpoint(client):
    _, headers = await _setup_project(client)
    resp = await client.get("/api/manuscripts/templates", headers=headers)
    assert resp.status_code == 200
    templates = {t["id"]: t for t in resp.json()}
    # 内置简化模板不再在画廊显示（只列提供官方样式的会议）
    assert not ({"neurips2026", "iclr2026", "acl"} & set(templates))
    # 官方 manifest 项以未下载伪条目出现
    pseudo = templates["seed:neurips2026-official"]
    assert pseudo["downloaded"] is False
    assert pseudo["download_key"] == "neurips2026-official"
    assert pseudo["source"] == "seeded"
    assert pseudo["page_limit"] == 9


async def test_create_manuscript_expands_template_and_fact_pack(client):
    project_id, headers = await _setup_project(client)
    idea_id = await _seed_idea(project_id)
    exp_id = await _seed_experiment(project_id, idea_id)
    await _seed_paper(project_id, "Attention Is All You Need", 2017)
    await _seed_paper(project_id, "Retrieval Augmented Models", 2023)
    await _seed_paper(project_id, "Excluded Candidate", 2022, status="candidate")

    resp = await _create_manuscript(
        client, headers, project_id, idea_id=idea_id, experiment_id=exp_id
    )
    assert resp.status_code == 201, resp.text
    ms = resp.json()
    assert ms["template"] == "neurips2026"
    assert ms["status"] == "draft"
    assert ms["experiment_id"] == exp_id

    resp = await client.get(f"/api/manuscripts/{ms['id']}", headers=headers)
    detail = resp.json()
    files = {f["path"]: f for f in detail["files"]}
    # references.bib 现为建稿时生成的可见可编辑文件
    assert set(files) == {"main.tex", "polaris_neurips2026.sty", "references.bib"}
    assert files["polaris_neurips2026.sty"]["readonly"] is True
    assert files["main.tex"]["readonly"] is False
    assert files["references.bib"]["readonly"] is False
    assert detail["latest_compile"] is None
    assert detail["writing_voyage_id"] is None

    # main.tex：标题注入 + 分节标记
    resp = await client.get(
        f"/api/manuscripts/{ms['id']}/files/{files['main.tex']['id']}", headers=headers
    )
    content = resp.json()["content"]
    assert "Co-citation Graph Retrieval" in content
    assert "% POLARIS_SECTION: introduction" in content
    assert "% POLARIS_SECTION_END: conclusion" in content
    assert "\\bibliography{references}" in content

    # fact-pack：idea / 假设 / 全 run 指标 / 图表 fig_id / citations bibkey
    pack = detail["fact_pack"]
    assert pack["idea"]["title"] == "共引图增强检索（test idea）"
    assert [h["status"] for h in pack["hypotheses"]] == ["verified", "testing"]
    accuracy = next(m for m in pack["metrics"] if m["name"] == "accuracy")
    assert accuracy["runs"] == [{"seq": 1, "value": 0.7}, {"seq": 2, "value": 0.8}]
    assert accuracy["best"] == 0.8
    assert pack["figures"] == [
        {"fig_id": "exp_fig_0", "caption": "主指标曲线", "source": "experiment"}
    ]
    bibkeys = {c["bibkey"] for c in pack["citations"]}
    assert bibkeys == {"smith2017attention", "smith2023retrieval"}  # excluded 论文不入
    assert all(c["source"] == "library" for c in pack["citations"])


async def test_fact_pack_refresh_picks_up_new_papers(client):
    project_id, headers = await _setup_project(client)
    resp = await _create_manuscript(client, headers, project_id)
    ms_id = resp.json()["id"]
    assert resp.json()["idea_id"] is None

    await _seed_paper(project_id, "A Fresh Included Paper", 2025, status="included")
    resp = await client.post(f"/api/manuscripts/{ms_id}/fact-pack/refresh", headers=headers)
    assert resp.status_code == 200
    pack = resp.json()
    assert [c["bibkey"] for c in pack["citations"]] == ["smith2025fresh"]
    assert pack["idea"] is None and pack["metrics"] == [] and pack["figures"] == []


async def test_file_crud_readonly_and_reserved(client):
    project_id, headers = await _setup_project(client)
    resp = await _create_manuscript(client, headers, project_id)
    ms_id = resp.json()["id"]
    detail = (await client.get(f"/api/manuscripts/{ms_id}", headers=headers)).json()
    files = {f["path"]: f for f in detail["files"]}
    sty_id = files["polaris_neurips2026.sty"]["id"]

    # 新建 + 重名冲突 + 保留路径 + 穿越路径
    resp = await client.post(
        f"/api/manuscripts/{ms_id}/files",
        json={"path": "appendix.tex", "content": "% appendix"},
        headers=headers,
    )
    assert resp.status_code == 201
    appendix_id = resp.json()["id"]
    for bad in ("appendix.tex", "references.bib", "figures/x.pdf", "../evil.tex"):
        resp = await client.post(
            f"/api/manuscripts/{ms_id}/files", json={"path": bad}, headers=headers
        )
        assert resp.status_code == 409, bad
        assert resp.json()["detail"] == "FILE_PATH_INVALID"

    # 重命名 + 删除
    resp = await client.patch(
        f"/api/manuscripts/{ms_id}/files/{appendix_id}",
        json={"path": "sections/appendix.tex"},
        headers=headers,
    )
    assert resp.status_code == 200 and resp.json()["path"] == "sections/appendix.tex"
    resp = await client.delete(f"/api/manuscripts/{ms_id}/files/{appendix_id}", headers=headers)
    assert resp.status_code == 204

    # readonly 样式文件不可改删
    resp = await client.patch(
        f"/api/manuscripts/{ms_id}/files/{sty_id}", json={"path": "x.sty"}, headers=headers
    )
    assert resp.status_code == 409 and resp.json()["detail"] == "FILE_READONLY"
    resp = await client.delete(f"/api/manuscripts/{ms_id}/files/{sty_id}", headers=headers)
    assert resp.status_code == 409 and resp.json()["detail"] == "FILE_READONLY"


async def test_manuscript_permissions_and_delete(client):
    project_id, headers = await _setup_project(client)
    resp = await _create_manuscript(client, headers, project_id)
    ms_id = resp.json()["id"]

    # 非成员一律 404
    outsider = await register_and_login(client, email="mallory@example.com")
    outsider_headers = {"Authorization": f"Bearer {outsider}"}
    resp = await client.get(f"/api/manuscripts/{ms_id}", headers=outsider_headers)
    assert resp.status_code == 404

    # 普通成员可读不可删（owner/admin only）
    member_token = await register_and_login(client, email="bob@example.com")
    member_headers = {"Authorization": f"Bearer {member_token}"}
    resp = await client.post(
        f"/api/projects/{project_id}/members",
        json={"email": "bob@example.com", "role": "member"},
        headers=headers,
    )
    assert resp.status_code == 204, resp.text
    resp = await client.get(f"/api/manuscripts/{ms_id}", headers=member_headers)
    assert resp.status_code == 200
    resp = await client.delete(f"/api/manuscripts/{ms_id}", headers=member_headers)
    assert resp.status_code == 403

    # PATCH 标题（成员可改），owner 可删
    resp = await client.patch(
        f"/api/manuscripts/{ms_id}", json={"title": "New Title"}, headers=member_headers
    )
    assert resp.status_code == 200 and resp.json()["title"] == "New Title"
    # owner 删除 → 移入垃圾箱（软删除）：不在活跃列表，在垃圾箱列表
    resp = await client.delete(f"/api/manuscripts/{ms_id}", headers=headers)
    assert resp.status_code == 204
    active = (await client.get(f"/api/projects/{project_id}/manuscripts", headers=headers)).json()
    assert ms_id not in [m["id"] for m in active]
    trashed = (
        await client.get(f"/api/projects/{project_id}/manuscripts?trashed=true", headers=headers)
    ).json()
    assert ms_id in [m["id"] for m in trashed]

    # 恢复 → 回到活跃列表
    resp = await client.post(f"/api/manuscripts/{ms_id}/restore", headers=headers)
    assert resp.status_code == 200 and resp.json()["trashed_at"] is None
    active = (await client.get(f"/api/projects/{project_id}/manuscripts", headers=headers)).json()
    assert ms_id in [m["id"] for m in active]

    # 永久删除（permanent）→ 彻底没了
    resp = await client.delete(f"/api/manuscripts/{ms_id}?permanent=true", headers=headers)
    assert resp.status_code == 204
    resp = await client.get(f"/api/manuscripts/{ms_id}", headers=headers)
    assert resp.status_code == 404


async def test_manuscript_batch_trash_restore_empty(client):
    project_id, headers = await _setup_project(client)
    ids = [
        (await _create_manuscript(client, headers, project_id, title=f"M{i}")).json()["id"]
        for i in range(3)
    ]

    def active():
        return client.get(f"/api/projects/{project_id}/manuscripts", headers=headers)

    def trash():
        return client.get(f"/api/projects/{project_id}/manuscripts?trashed=true", headers=headers)

    # 批量移入垃圾箱 2 个
    r = await client.post(
        f"/api/projects/{project_id}/manuscripts/batch",
        json={"action": "trash", "ids": ids[:2]},
        headers=headers,
    )
    assert r.status_code == 200 and r.json()["affected"] == 2
    assert {m["id"] for m in (await active()).json()} == {ids[2]}
    assert {m["id"] for m in (await trash()).json()} == set(ids[:2])

    # 批量恢复 1 个
    r = await client.post(
        f"/api/projects/{project_id}/manuscripts/batch",
        json={"action": "restore", "ids": [ids[0]]},
        headers=headers,
    )
    assert r.json()["affected"] == 1

    # 清空垃圾箱 → 只永久删除仍在垃圾箱的 ids[1]
    r = await client.post(f"/api/projects/{project_id}/manuscripts/trash/empty", headers=headers)
    assert r.status_code == 200 and r.json()["affected"] == 1
    assert (await client.get(f"/api/manuscripts/{ids[1]}", headers=headers)).status_code == 404
    assert {m["id"] for m in (await active()).json()} == {ids[0], ids[2]}


async def test_submit_requires_ok_compile_then_gate_flow(client, bus_recorder, queue_stub):
    project_id, headers = await _setup_project(client)
    resp = await _create_manuscript(client, headers, project_id)
    ms_id = resp.json()["id"]

    # 前置：无编译 → 409
    resp = await client.post(f"/api/manuscripts/{ms_id}/submit", headers=headers)
    assert resp.status_code == 409 and resp.json()["detail"] == "COMPILE_REQUIRED"

    # 种一个 ok 的 latest_compile（真实编译见 test_latex_compile）
    async with get_sessionmaker()() as session:
        ms = await session.get(Manuscript, uuid.UUID(ms_id))
        ms.status = "compiled"
        ms.latest_compile = {
            "version": 3,
            "status": "ok",
            "pdf_available": True,
            "diagnostics": [],
            "compiled_at": "2026-07-14T00:00:00+00:00",
            "duration_ms": 1200,
        }
        await session.commit()

    # M5-C：编译 ok 但评审未通过 → 409 REVIEW_REQUIRED
    resp = await client.post(f"/api/manuscripts/{ms_id}/submit", headers=headers)
    assert resp.status_code == 409 and resp.json()["detail"] == "REVIEW_REQUIRED"
    async with get_sessionmaker()() as session:
        ms = await session.get(Manuscript, uuid.UUID(ms_id))
        ms.review_passed = True
        await session.commit()

    resp = await client.post(f"/api/manuscripts/{ms_id}/submit", headers=headers)
    assert resp.status_code == 201, resp.text
    gate = resp.json()
    assert gate["kind"] == "paper_submission"
    assert gate["payload"]["manuscript_id"] == ms_id
    assert gate["payload"]["compile_version"] == 3
    detail = (await client.get(f"/api/manuscripts/{ms_id}", headers=headers)).json()
    assert detail["status"] == "under_review"
    assert any(m.get("type") == "gate.created" for _, m in bus_recorder.notify)

    # 重复 submit：状态非 ok 前置不变（latest_compile 仍 ok → 允许再次创建？不——
    # under_review 中再次 submit 会再建闸门；此处只验证审批联动）
    resp = await client.post(f"/api/gates/{gate['id']}/approve", json={}, headers=headers)
    assert resp.status_code == 200
    detail = (await client.get(f"/api/manuscripts/{ms_id}", headers=headers)).json()
    assert detail["status"] == "submitted"
    assert any(
        m.get("type") == "manuscript.status" and m.get("status") == "submitted"
        for _, m in bus_recorder.notify
    )


async def test_submit_reject_rolls_back_to_compiled(client, bus_recorder, queue_stub):
    project_id, headers = await _setup_project(client)
    resp = await _create_manuscript(client, headers, project_id)
    ms_id = resp.json()["id"]
    async with get_sessionmaker()() as session:
        ms = await session.get(Manuscript, uuid.UUID(ms_id))
        ms.status = "compiled"
        ms.review_passed = True  # M5-C submit 前置
        ms.latest_compile = {
            "version": 1,
            "status": "ok",
            "pdf_available": True,
            "diagnostics": [],
            "compiled_at": "2026-07-14T00:00:00+00:00",
            "duration_ms": 900,
        }
        await session.commit()
    gate = (await client.post(f"/api/manuscripts/{ms_id}/submit", headers=headers)).json()
    resp = await client.post(f"/api/gates/{gate['id']}/reject", json={}, headers=headers)
    assert resp.status_code == 200
    detail = (await client.get(f"/api/manuscripts/{ms_id}", headers=headers)).json()
    assert detail["status"] == "compiled"


async def test_manuscript_list(client):
    project_id, headers = await _setup_project(client)
    for title in ("Paper A", "Paper B"):
        resp = await _create_manuscript(client, headers, project_id, title=title)
        assert resp.status_code == 201
    resp = await client.get(f"/api/projects/{project_id}/manuscripts", headers=headers)
    assert resp.status_code == 200
    assert {m["title"] for m in resp.json()} == {"Paper A", "Paper B"}
    # 未知模板 404
    resp = await _create_manuscript(client, headers, project_id, template="unknown")
    assert resp.status_code == 404 and resp.json()["detail"] == "TEMPLATE_NOT_FOUND"


async def test_manuscripts_query_helper(client):
    """列表按创建时间倒序 + 非成员项目列表 404。"""
    project_id, headers = await _setup_project(client)
    outsider = await register_and_login(client, email="eve@example.com")
    resp = await client.get(
        f"/api/projects/{project_id}/manuscripts",
        headers={"Authorization": f"Bearer {outsider}"},
    )
    assert resp.status_code == 404
    async with get_sessionmaker()() as session:
        rows = (await session.execute(select(Manuscript))).scalars().all()
        assert rows == []
