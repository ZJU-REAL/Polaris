"""稿件文件版本历史测试：编译/AI 写入自动打点、去重、列表、恢复（含 pre_restore 备份）。"""

import uuid
from pathlib import Path

import pytest_asyncio

from app.core.db import get_sessionmaker
from app.models.manuscript import ManuscriptFile
from app.services import latex_compile
from app.services.crdt_rooms import get_crdt_rooms
from app.services.latex_compile import TectonicRun
from tests.test_manuscripts import _create_manuscript, _setup_project


@pytest_asyncio.fixture(autouse=True)
async def _clean_crdt():
    from app.services.crdt_rooms import reset_crdt_rooms

    yield
    await reset_crdt_rooms()


@pytest_asyncio.fixture(autouse=True)
def _stub_tectonic(monkeypatch):
    def ok_run(binary: str, workdir: Path) -> TectonicRun:
        (workdir / "main.pdf").write_bytes(b"%PDF stub")
        (workdir / "main.log").write_text("", encoding="utf-8")
        return TectonicRun(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(latex_compile, "_find_tectonic", lambda: "/usr/bin/tectonic")
    monkeypatch.setattr(latex_compile, "_run_tectonic", ok_run)


async def _setup(client):
    project_id, headers = await _setup_project(client)
    resp = await _create_manuscript(client, headers, project_id)
    ms_id = resp.json()["id"]
    detail = (await client.get(f"/api/manuscripts/{ms_id}", headers=headers)).json()
    main = next(f for f in detail["files"] if f["path"] == "main.tex")
    return headers, ms_id, main["id"]


async def test_compile_snapshots_versions_and_dedupes(client):
    headers, ms_id, fid = await _setup(client)

    resp = await client.get(f"/api/manuscripts/{ms_id}/files/{fid}/versions", headers=headers)
    assert resp.status_code == 200 and resp.json() == []

    # 第一次编译 → 每个可写文件打一份 compile 快照
    assert (
        await client.post(f"/api/manuscripts/{ms_id}/compile", headers=headers)
    ).status_code == 200
    versions = (
        await client.get(f"/api/manuscripts/{ms_id}/files/{fid}/versions", headers=headers)
    ).json()
    assert len(versions) == 1
    assert versions[0]["origin"] == "compile"
    assert versions[0]["label"] == "编译 v1"
    assert versions[0]["seq"] == 1

    # 内容没变的第二次编译 → 不重复打点
    assert (
        await client.post(f"/api/manuscripts/{ms_id}/compile", headers=headers)
    ).status_code == 200
    versions = (
        await client.get(f"/api/manuscripts/{ms_id}/files/{fid}/versions", headers=headers)
    ).json()
    assert len(versions) == 1


async def test_ai_edit_snapshots_pre_ai_version(client):
    headers, ms_id, fid = await _setup(client)
    await get_crdt_rooms().apply_ai_edit(uuid.UUID(fid), "introduction", "New intro body.")

    versions = (
        await client.get(f"/api/manuscripts/{ms_id}/files/{fid}/versions", headers=headers)
    ).json()
    assert len(versions) == 1
    assert versions[0]["origin"] == "pre_ai"
    assert "introduction" in versions[0]["label"]
    # 快照存的是写入前内容
    v = (
        await client.get(
            f"/api/manuscripts/{ms_id}/files/{fid}/versions/{versions[0]['id']}", headers=headers
        )
    ).json()
    assert "（待撰写 / to be drafted）" in v["content"]
    assert "New intro body." not in v["content"]


async def test_restore_version_backs_up_current(client):
    headers, ms_id, fid = await _setup(client)
    # 原始内容 → AI 写入（产生 pre_ai 快照）
    await get_crdt_rooms().apply_ai_edit(uuid.UUID(fid), "introduction", "AI wrote this.")
    versions = (
        await client.get(f"/api/manuscripts/{ms_id}/files/{fid}/versions", headers=headers)
    ).json()
    pre_ai = versions[0]

    # 恢复到 AI 写入前
    resp = await client.post(
        f"/api/manuscripts/{ms_id}/files/{fid}/versions/{pre_ai['id']}/restore", headers=headers
    )
    assert resp.status_code == 200
    assert "AI wrote this." not in resp.json()["content"]

    async with get_sessionmaker()() as session:
        file = await session.get(ManuscriptFile, uuid.UUID(fid))
        assert "AI wrote this." not in file.content

    # 恢复前的当前内容已备份为 pre_restore
    versions = (
        await client.get(f"/api/manuscripts/{ms_id}/files/{fid}/versions", headers=headers)
    ).json()
    origins = [v["origin"] for v in versions]
    assert "pre_restore" in origins
    backup = next(v for v in versions if v["origin"] == "pre_restore")
    b = (
        await client.get(
            f"/api/manuscripts/{ms_id}/files/{fid}/versions/{backup['id']}", headers=headers
        )
    ).json()
    assert "AI wrote this." in b["content"]


async def test_restore_readonly_rejected_and_version_404(client):
    headers, ms_id, fid = await _setup(client)
    detail = (await client.get(f"/api/manuscripts/{ms_id}", headers=headers)).json()
    sty = next(f for f in detail["files"] if f["path"].endswith(".sty"))

    resp = await client.post(
        f"/api/manuscripts/{ms_id}/files/{sty['id']}/versions/{uuid.uuid4()}/restore",
        headers=headers,
    )
    assert resp.status_code == 409
    assert resp.json()["detail"] == "FILE_READONLY"

    resp = await client.post(
        f"/api/manuscripts/{ms_id}/files/{fid}/versions/{uuid.uuid4()}/restore", headers=headers
    )
    assert resp.status_code == 404
    assert resp.json()["detail"] == "VERSION_NOT_FOUND"
