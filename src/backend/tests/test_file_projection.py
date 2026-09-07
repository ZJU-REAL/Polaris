"""常驻文件投影（#719 file-over-app 一期）：布局 / 双写钩子 / 回退与清理 / 回填 CLI。

投影默认在测试套件关（conftest），本文件用 projection_on fixture 单独开，
并把 data_dir 指到独立 tmp_path，互不污染。
"""

import os
import uuid
from argparse import Namespace
from pathlib import Path

import pytest

from app.core.config import get_settings
from app.core.db import get_sessionmaker
from tests.conftest import add_paper, ensure_project_library, register_and_login

pytestmark = pytest.mark.asyncio

FAKE_PDF = b"%PDF-1.4 fake body for projection tests"


@pytest.fixture
def projection_on(monkeypatch, tmp_path):
    """开投影 + 独立 data_dir（get_settings 是 lru_cache 的，改 env 后须清缓存）。"""
    monkeypatch.setenv("POLARIS_FILE_PROJECTION", "1")
    monkeypatch.setenv("POLARIS_DATA_DIR", str(tmp_path / "data"))
    get_settings.cache_clear()
    yield tmp_path / "data"
    get_settings.cache_clear()


async def _make_project(client):
    token = await register_and_login(client)
    headers = {"Authorization": f"Bearer {token}"}
    resp = await client.post("/api/projects", json={"name": "proj-projection"}, headers=headers)
    return resp.json()["id"], headers


def _attach_fake_pdf(paper) -> Path:
    """按内部约定造 uuid 原件（<data_dir>/papers/<id>.pdf）并接到 paper 上。"""
    from app.services.literature.pdf_extract import papers_dir

    src = papers_dir() / f"{paper.id}.pdf"
    src.write_bytes(FAKE_PDF)
    paper.pdf_path = str(src)
    return src


async def test_pdf_projection_is_a_hardlink_with_citekey_name(client, projection_on):
    from app.services import file_projection

    project_id, _ = await _make_project(client)
    async with get_sessionmaker()() as session:
        paper = await add_paper(
            session,
            project_id=uuid.UUID(project_id),
            title="Deep Residual Learning",
            authors=[{"name": "Kaiming He"}],
            year=2016,
            status="included",
        )
        src = _attach_fake_pdf(paper)
        await session.commit()

        target = file_projection.project_paper_pdf(paper)

    assert target is not None
    # citekey 与全量导出 papers.bib 同源：he2016deep
    assert target.name == "he2016deep - Deep Residual Learning.pdf"
    assert target.parent == projection_on / "workspace" / "papers"
    # 硬链接：同一 inode，原件不动（内部路径引用全指向 uuid 原件）
    assert os.path.samefile(target, src)
    assert src.is_file()
    # 顶层 README（诚实边界说明）随首次投影落位
    readme = (projection_on / "workspace" / "README.md").read_text(encoding="utf-8")
    assert "真源" in readme

    # 幂等：重复投影不报错、还是同一个文件
    async with get_sessionmaker()() as session:
        paper2 = await session.get(type(paper), paper.id)
        assert file_projection.project_paper_pdf(paper2) == target


async def test_pdf_projection_falls_back_to_copy(client, projection_on, monkeypatch):
    from app.services import file_projection

    def _no_link(*args, **kwargs):
        raise OSError("cross-device link")

    monkeypatch.setattr(os, "link", _no_link)
    project_id, _ = await _make_project(client)
    async with get_sessionmaker()() as session:
        paper = await add_paper(
            session, project_id=uuid.UUID(project_id), title="Copy Fallback Paper"
        )
        src = _attach_fake_pdf(paper)
        await session.commit()
        target = file_projection.project_paper_pdf(paper)
    assert target is not None and target.is_file()
    assert target.read_bytes() == FAKE_PDF
    assert not os.path.samefile(target, src)  # 复制而非硬链接


async def test_filenames_are_sanitized_and_truncated(client, projection_on):
    from app.services import file_projection

    project_id, _ = await _make_project(client)
    nasty = 'A/B\\C:D*E?F"G<H>I|J' + "超长标题" * 60  # 特殊字符 + 远超 80 字符
    async with get_sessionmaker()() as session:
        paper = await add_paper(session, project_id=uuid.UUID(project_id), title=nasty)
        name = file_projection.paper_pdf_filename(paper)
    assert not set(name) & set('/\\:*?"<>|')
    # 标题成分截到 80 字符以内（与全量导出同一把尺子）
    title_part = name.removesuffix(".pdf").split(" - ", 1)[1]
    assert len(title_part) <= 80


async def test_note_and_highlight_hooks_keep_the_notes_file_fresh(client, projection_on):
    project_id, headers = await _make_project(client)
    async with get_sessionmaker()() as session:
        paper = await add_paper(
            session, project_id=uuid.UUID(project_id), title="Projected Paper", year=2026
        )
        await session.commit()
        paper_id = str(paper.id)

    notes_file = projection_on / "workspace" / "notes" / "Projected Paper.md"

    # 建笔记 → 文件出现且含内容与 frontmatter
    resp = await client.post(
        f"/api/papers/{paper_id}/notes", json={"content": "重点：方法部分"}, headers=headers
    )
    assert resp.status_code == 201
    note_id = resp.json()["id"]
    body = notes_file.read_text(encoding="utf-8")
    assert "重点：方法部分" in body
    assert 'title: "Projected Paper"' in body

    # 改笔记 → 文件跟着变
    await client.patch(f"/api/notes/{note_id}", json={"content": "改过了"}, headers=headers)
    body = notes_file.read_text(encoding="utf-8")
    assert "改过了" in body and "重点：方法部分" not in body

    # 加划线 → 同一文件的「划线」小节
    resp = await client.post(
        f"/api/papers/{paper_id}/highlights",
        json={
            "page": 3,
            "rects": [{"x0": 0.1, "y0": 0.1, "x1": 0.5, "y1": 0.2}],
            "selected_text": "the projected sentence",
        },
        headers=headers,
    )
    assert resp.status_code == 201
    hl_id = resp.json()["id"]
    body = notes_file.read_text(encoding="utf-8")
    assert "p.3: > the projected sentence" in body

    # 删笔记 → 还剩划线，文件保留但笔记小节消失
    await client.delete(f"/api/notes/{note_id}", headers=headers)
    body = notes_file.read_text(encoding="utf-8")
    assert "改过了" not in body and "the projected sentence" in body

    # 删最后一条划线 → 文件整个清走
    await client.delete(f"/api/highlights/{hl_id}", headers=headers)
    assert not notes_file.exists()


async def test_wiki_vault_projection_renders_an_obsidian_vault(client, projection_on):
    from app.services import file_projection

    project_id, _ = await _make_project(client)
    async with get_sessionmaker()() as session:
        await add_paper(
            session,
            project_id=uuid.UUID(project_id),
            title="Compiled Paper",
            status="compiled",
            wiki_content="## TL;DR\n\n投影用解读正文。\n",
        )
        library = await ensure_project_library(session, uuid.UUID(project_id))
        await session.commit()
        await file_projection.refresh_library_vault(session, library)
        vault = projection_on / "workspace" / "wiki" / library.name

    assert (vault / "index.md").is_file()
    pages = list((vault / "papers").glob("*.md"))
    assert len(pages) == 1
    assert "投影用解读正文" in pages[0].read_text(encoding="utf-8")

    # 按论文反查刷新（编译钩子走这条）：重建后 vault 仍在
    async with get_sessionmaker()() as session:
        from sqlalchemy import select

        from app.models.paper import Paper

        paper = (
            await session.execute(select(Paper).where(Paper.title == "Compiled Paper"))
        ).scalar_one()
        await file_projection.refresh_wiki_vaults_for_paper(session, paper.id)
    assert (vault / "index.md").is_file()


async def test_remove_paper_projection_cleans_pdf_and_notes(client, projection_on):
    from app.services import file_projection

    project_id, headers = await _make_project(client)
    async with get_sessionmaker()() as session:
        paper = await add_paper(session, project_id=uuid.UUID(project_id), title="Doomed Paper")
        _attach_fake_pdf(paper)
        await session.commit()
        target = file_projection.project_paper_pdf(paper)
        paper_id = str(paper.id)
    await client.post(
        f"/api/papers/{paper_id}/notes", json={"content": "临终笔记"}, headers=headers
    )
    notes_file = projection_on / "workspace" / "notes" / "Doomed Paper.md"
    assert target.is_file() and notes_file.is_file()

    async with get_sessionmaker()() as session:
        from app.models.paper import Paper

        paper = await session.get(Paper, uuid.UUID(paper_id))
        file_projection.remove_paper_projection(paper)
    assert not target.exists()
    assert not notes_file.exists()


async def test_projection_disabled_by_default_no_side_files(client):
    """conftest 关着开关（golden 链同环境）：业务写路径不产生 workspace 旁路文件。"""
    project_id, headers = await _make_project(client)
    async with get_sessionmaker()() as session:
        paper = await add_paper(session, project_id=uuid.UUID(project_id), title="Silent Paper")
        await session.commit()
        paper_id = str(paper.id)
    resp = await client.post(
        f"/api/papers/{paper_id}/notes", json={"content": "无投影"}, headers=headers
    )
    assert resp.status_code == 201
    assert not (Path(os.environ["POLARIS_DATA_DIR"]) / "workspace").exists()


async def test_db_wins_conflict_rule_is_documented():
    """一期诚实边界必须写在代码里：DB wins、投影不回写。"""
    from app.services import file_projection

    assert "DB wins" in (file_projection.__doc__ or "")
    assert "不会回写" in (file_projection.__doc__ or "")


async def test_backfill_cli_renders_existing_rows(client, projection_on, capsys):
    from app.cli.build_file_projection import _run

    project_id, headers = await _make_project(client)
    async with get_sessionmaker()() as session:
        paper = await add_paper(
            session,
            project_id=uuid.UUID(project_id),
            title="Backfill Paper",
            status="compiled",
            wiki_content="## TL;DR\n\n回填解读。\n",
        )
        _attach_fake_pdf(paper)
        await session.commit()
        paper_id = str(paper.id)
    # 存量笔记（钩子建过的文件先删掉，验证 CLI 能重建）
    await client.post(
        f"/api/papers/{paper_id}/notes", json={"content": "存量笔记"}, headers=headers
    )
    workspace = projection_on / "workspace"
    (workspace / "notes" / "Backfill Paper.md").unlink()

    await _run(Namespace(library=None, force=False))

    out = capsys.readouterr().out
    assert "papers: 1/1" in out
    assert (workspace / "notes" / "Backfill Paper.md").read_text(encoding="utf-8").count("存量笔记")
    assert list((workspace / "papers").glob("*.pdf"))
    assert list((workspace / "wiki").iterdir())
