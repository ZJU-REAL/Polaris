"""库作用域能力补齐（PR-1）：独立库（project_id=NULL）的 Obsidian 导出 /
全文索引重建 / 概念补建，以及论文 schema 的 library_id 字段。

课题作用域的同名端点不受影响（见 test_wiki_api / test_library_chat / test_concepts_relink）。
"""

import io
import tempfile
import uuid
import zipfile
from pathlib import Path

from sqlalchemy import select

from app.core.db import get_sessionmaker
from app.models.library_direction import LibraryPaper
from app.models.paper import Paper
from app.models.user import User
from tests.conftest import register_and_login


async def _hdr(client, email):
    return {"Authorization": f"Bearer {await register_and_login(client, email=email)}"}


async def _promote_admin(email: str) -> None:
    async with get_sessionmaker()() as session:
        user = (await session.execute(select(User).where(User.email == email))).scalar_one()
        user.role = "admin"
        await session.commit()


async def _setup(client, *, prefix):
    """admin + 创建者 + 一个 active 独立库（project_id 为空）；返回 (creator, admin, lib_id)。"""
    admin = await _hdr(client, f"{prefix}-admin@example.com")
    await _promote_admin(f"{prefix}-admin@example.com")
    creator = await _hdr(client, f"{prefix}-owner@example.com")
    resp = await client.post(
        "/api/libraries",
        json={"name": "独立库", "statement": "LLM agent 规划方向"},
        headers=creator,
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["project_id"] is None
    lib_id = resp.json()["id"]
    resp = await client.post(f"/api/libraries/{lib_id}/approve", headers=admin)
    assert resp.status_code == 200, resp.text
    return creator, admin, lib_id


async def _seed_paper(
    lib_id,
    *,
    title,
    status="compiled",
    relevance=0.8,
    wiki=None,
    full_text_path=None,
):
    async with get_sessionmaker()() as session:
        paper = Paper(
            title=title,
            abstract=f"{title} abstract",
            authors=[{"name": "Alice"}],
            year=2025,
            source="arxiv",
            full_text_path=full_text_path,
        )
        session.add(paper)
        await session.flush()
        session.add(
            LibraryPaper(
                library_id=uuid.UUID(str(lib_id)),
                paper_id=paper.id,
                status=status,
                relevance_score=relevance,
                wiki_content=wiki,
            )
        )
        await session.commit()
        return str(paper.id)


# ---- 1. Obsidian 导出 ----


async def test_standalone_library_obsidian_export(client):
    creator, _admin, lib_id = await _setup(client, prefix="libscope-obs")
    await _seed_paper(
        lib_id,
        title="Planning with Tree Search",
        wiki="本文提出 [[树搜索]]，结合 [[强化学习]] 训练。",
    )
    await _seed_paper(lib_id, title="Trashed paper", status="excluded")
    # 概念页要有内容 → 先补建概念
    resp = await client.post(f"/api/libraries/{lib_id}/concepts/relink", headers=creator)
    assert resp.status_code == 200, resp.text

    resp = await client.get(f"/api/libraries/{lib_id}/export/obsidian", headers=creator)
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"] == "application/zip"
    assert "polaris-library-wiki.zip" in resp.headers["content-disposition"]

    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        names = zf.namelist()
        assert "index.md" in names and "trends.md" in names
        index = zf.read("index.md").decode()
        assert "独立库 · Research Wiki" in index
        paper_pages = [n for n in names if n.startswith("papers/") and n.endswith(".md")]
        assert len(paper_pages) == 1  # 垃圾桶论文不导出
        body = zf.read(paper_pages[0]).decode()
        assert "[[树搜索]]" in body  # 双链原样保留
        concept_pages = [n for n in names if n.startswith("concepts/")]
        assert len(concept_pages) == 2


async def test_library_obsidian_export_visible_to_readers(client):
    creator, _admin, lib_id = await _setup(client, prefix="libscope-obs-read")
    await _seed_paper(lib_id, title="Readable", wiki="正文 [[概念甲]]。")
    stranger = await _hdr(client, "libscope-obs-read-stranger@example.com")

    # 公共库只读端点：非管理者也能导出（与库作用域引用导出一致）
    resp = await client.get(f"/api/libraries/{lib_id}/export/obsidian", headers=stranger)
    assert resp.status_code == 200, resp.text
    assert len(resp.content) > 0
    # 库不存在 → 404
    resp = await client.get(f"/api/libraries/{uuid.uuid4()}/export/obsidian", headers=creator)
    assert resp.status_code == 404


# ---- 2. 全文索引重建 ----


async def test_standalone_library_index_rebuild(client):
    creator, _admin, lib_id = await _setup(client, prefix="libscope-idx")
    txt_dir = Path(tempfile.mkdtemp(prefix="polaris-libscope-"))
    for i, title in enumerate(["Planning survey", "Reflexion"]):
        txt = txt_dir / f"p{i}.txt"
        txt.write_text("规划方法的细节。" * 300, encoding="utf-8")
        await _seed_paper(lib_id, title=title, full_text_path=str(txt))
    # 没有全文的论文不参与
    await _seed_paper(lib_id, title="No fulltext")

    resp = await client.post(f"/api/libraries/{lib_id}/index/rebuild", headers=creator)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["papers_indexed"] == 2
    assert body["chunks_created"] > 0
    assert body["total_chunks"] == body["chunks_created"]
    assert body["embedded"] == body["chunks_created"] and body["embed_error"] is None

    # 幂等：再跑不重复建
    resp = await client.post(f"/api/libraries/{lib_id}/index/rebuild", headers=creator)
    assert resp.json()["papers_indexed"] == 0


# ---- 3. 概念补建 ----


async def test_standalone_library_concepts_relink(client):
    creator, _admin, lib_id = await _setup(client, prefix="libscope-rel")
    await _seed_paper(
        lib_id, title="Paper A", wiki="本文提出 [[自我博弈]]，结合 [[强化学习]] 训练。"
    )
    await _seed_paper(
        lib_id, title="Paper B", status="included", wiki="基于 [[强化学习]] 与 [[课程学习]]。"
    )
    await _seed_paper(lib_id, title="Paper C", status="candidate")  # 无 wiki 不参与

    resp = await client.post(f"/api/libraries/{lib_id}/concepts/relink", headers=creator)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["papers"] == 2
    assert body["concepts_created"] == 3
    assert body["links_created"] == 4  # A:2 + B:2（强化学习共享一个概念）

    resp = await client.get(f"/api/libraries/{lib_id}/concepts", headers=creator)
    counts = {c["name"]: c["paper_count"] for c in resp.json()}
    assert counts == {"自我博弈": 1, "强化学习": 2, "课程学习": 1}

    # 幂等
    resp = await client.post(f"/api/libraries/{lib_id}/concepts/relink", headers=creator)
    assert resp.json()["concepts_created"] == 0 and resp.json()["links_created"] == 0


# ---- 4. 维护端点鉴权 ----


async def test_library_maintenance_endpoints_forbidden_for_non_manager(client):
    creator, _admin, lib_id = await _setup(client, prefix="libscope-403")
    await _seed_paper(lib_id, title="Guarded", wiki="正文 [[概念甲]]。")
    stranger = await _hdr(client, "libscope-403-stranger@example.com")

    resp = await client.post(f"/api/libraries/{lib_id}/concepts/relink", headers=stranger)
    assert resp.status_code == 403
    resp = await client.post(f"/api/libraries/{lib_id}/index/rebuild", headers=stranger)
    assert resp.status_code == 403
    # 库不存在 → 404（管理端点同样不泄漏）
    resp = await client.post(f"/api/libraries/{uuid.uuid4()}/index/rebuild", headers=creator)
    assert resp.status_code == 404
    resp = await client.post(f"/api/libraries/{uuid.uuid4()}/concepts/relink", headers=creator)
    assert resp.status_code == 404


# ---- 5. PaperRead.library_id ----


async def test_paper_read_exposes_library_id(client):
    creator, _admin, lib_id = await _setup(client, prefix="libscope-field")
    paper_id = await _seed_paper(lib_id, title="Field carrier", wiki="正文。")

    resp = await client.get(f"/api/libraries/{lib_id}/papers", headers=creator)
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    assert [i["library_id"] for i in items] == [lib_id]

    # 单篇详情（库工作台 + 阅读页两条路径）
    resp = await client.get(f"/api/libraries/{lib_id}/papers/{paper_id}", headers=creator)
    assert resp.json()["library_id"] == lib_id
    resp = await client.get(f"/api/papers/{paper_id}", headers=creator)
    assert resp.status_code == 200, resp.text
    assert resp.json()["library_id"] == lib_id
