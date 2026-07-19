"""官方模板按需自动下载 + 进度：manifest 未下载项展示 / 触发下载 / SSE 进度 / 建稿。"""

import asyncio

import pytest
import pytest_asyncio

from app.services import manuscript_templates as templates
from tests.test_manuscripts import _setup_project

KEY = "neurips2026-official"  # SEED_MANIFEST 里的一个 zip 源


@pytest_asyncio.fixture(autouse=True)
def _clear_progress():
    templates._download_progress.clear()
    templates._download_locks.clear()
    yield
    templates._download_progress.clear()
    templates._download_locks.clear()


@pytest.fixture
def _fake_fetch(monkeypatch):
    """把 zip 拉取换成本地假数据，并触发进度回调（不联网）。"""

    async def fake_zip(url, on_percent=None):
        if on_percent:
            on_percent(40)
            on_percent(99)
        return {
            "neurips_2026.sty": b"% neurips style",
            "main.tex": (
                b"\\documentclass{article}\n\\usepackage{neurips_2026}\n"
                b"\\begin{document}\n{{POLARIS_TITLE}}\n\\end{document}\n"
            ),
        }

    monkeypatch.setattr(templates, "_fetch_zip_members", fake_zip)


async def _wait_done(key: str, timeout: float = 5.0) -> dict:
    for _ in range(int(timeout / 0.05)):
        p = templates.get_progress(key)
        if p and p["phase"] in ("done", "failed"):
            return p
        await asyncio.sleep(0.05)
    raise AssertionError(f"下载未在 {timeout}s 内结束：{templates.get_progress(key)}")


async def test_manifest_shows_as_not_downloaded(client, _fake_fetch):
    _, headers = await _setup_project(client)
    resp = await client.get("/api/manuscripts/templates", headers=headers)
    assert resp.status_code == 200
    by_id = {t["id"]: t for t in resp.json()}
    # 未下载的官方模板以 seed:<key> 伪条目出现
    pseudo = by_id.get(f"seed:{KEY}")
    assert pseudo is not None
    assert pseudo["downloaded"] is False
    assert pseudo["download_key"] == KEY
    assert pseudo["source"] == "seeded"
    # 内置简化模板不再在画廊显示
    assert "neurips2026" not in by_id


async def test_download_then_create_manuscript(client, _fake_fetch):
    project_id, headers = await _setup_project(client)

    # 触发按需下载
    resp = await client.post(f"/api/manuscripts/templates/download/{KEY}", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["key"] == KEY

    # 等后台任务完成
    prog = await _wait_done(KEY)
    assert prog["phase"] == "done"
    tpl_id = prog["template_id"]
    assert tpl_id

    # 列表里现在是真实模板（downloaded=true），伪条目消失
    resp = await client.get("/api/manuscripts/templates", headers=headers)
    by_id = {t["id"]: t for t in resp.json()}
    assert tpl_id in by_id and by_id[tpl_id]["downloaded"] is True
    assert f"seed:{KEY}" not in by_id

    # 用下载好的模板建稿：文件展开进项目、标题注入
    resp = await client.post(
        f"/api/projects/{project_id}/manuscripts",
        json={"title": "My NeurIPS Paper", "template": tpl_id},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    ms_id = resp.json()["id"]
    detail = (await client.get(f"/api/manuscripts/{ms_id}", headers=headers)).json()
    files = {f["path"] for f in detail["files"]}
    assert "main.tex" in files and "neurips_2026.sty" in files
    main = next(f for f in detail["files"] if f["path"] == "main.tex")
    content = (
        await client.get(f"/api/manuscripts/{ms_id}/files/{main['id']}", headers=headers)
    ).json()["content"]
    assert "My NeurIPS Paper" in content and "{{POLARIS_TITLE}}" not in content


async def test_download_is_idempotent(client, _fake_fetch):
    _, headers = await _setup_project(client)
    await client.post(f"/api/manuscripts/templates/download/{KEY}", headers=headers)
    await _wait_done(KEY)
    # 再次触发 → 直接 done，带同一模板 id
    resp = await client.post(f"/api/manuscripts/templates/download/{KEY}", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["phase"] == "done"
    assert resp.json()["template_id"]


async def test_progress_sse_emits_done(client, _fake_fetch):
    _, headers = await _setup_project(client)
    await client.post(f"/api/manuscripts/templates/download/{KEY}", headers=headers)
    body = ""
    async with client.stream(
        "GET", f"/api/manuscripts/templates/download/{KEY}/progress", headers=headers
    ) as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        async for chunk in resp.aiter_text():
            body += chunk
            if "event: done" in body or "event: error" in body:
                break
    events = [line for line in body.splitlines() if line.startswith("event:")]
    assert "event: progress" in events
    assert "event: done" in events


async def test_unknown_template_404(client):
    _, headers = await _setup_project(client)
    resp = await client.post("/api/manuscripts/templates/download/nope", headers=headers)
    assert resp.status_code == 404


async def test_ai_draft_on_official_template_no_invalid_sections(client, queue_stub, _fake_fetch):
    """会议官方模板不声明分节 → AI 起草应走兜底分节，不再报 INVALID_SECTIONS。"""
    project_id, headers = await _setup_project(client)
    await client.post(f"/api/manuscripts/templates/download/{KEY}", headers=headers)
    tpl_id = (await _wait_done(KEY))["template_id"]
    ms_id = (
        await client.post(
            f"/api/projects/{project_id}/manuscripts",
            json={"title": "P", "template": tpl_id},
            headers=headers,
        )
    ).json()["id"]

    # 全部节起草：201（不再 422 INVALID_SECTIONS）
    r = await client.post(f"/api/manuscripts/{ms_id}/draft", json={}, headers=headers)
    assert r.status_code == 201, r.text
    await client.post(f"/api/voyages/{r.json()['id']}/cancel", headers=headers)

    # 指定兜底分节里的某一节：201
    r = await client.post(
        f"/api/manuscripts/{ms_id}/draft", json={"sections": ["method"]}, headers=headers
    )
    assert r.status_code == 201, r.text
