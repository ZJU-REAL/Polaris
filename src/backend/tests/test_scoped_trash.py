"""库/课题作用域的垃圾桶召回 + 彻底删除。

fix：无库作用域的 /papers/{id}(/restore) 走跨库归并，会命中错库那份成员行——
彻底删除删不掉本库这份、召回也可能召回错库那份。这里验证作用域端点只动当前库。
"""

import uuid

from sqlalchemy import select

from app.core.db import get_sessionmaker
from app.models.library_direction import LibraryPaper
from app.models.paper import Paper
from tests.conftest import make_project_with_library
from tests.test_scoped_paper_detail import (
    _add_membership,
    _create_active_standalone,
    _hdr,
    _new_paper,
    _promote_admin,
)


async def _statuses_by_lib(paper_id: str) -> dict[str, str]:
    async with get_sessionmaker()() as session:
        rows = (
            await session.execute(
                select(LibraryPaper).where(LibraryPaper.paper_id == uuid.UUID(paper_id))
            )
        ).scalars().all()
        return {str(r.library_id): r.status for r in rows}


async def test_scoped_purge_only_removes_current_library_membership(client):
    """A 库在库、B 库垃圾桶：从 B 库彻底删除只删 B 那份，A 那份保留，池论文保留。"""
    admin = await _hdr(client, "purge-admin@example.com")
    await _promote_admin("purge-admin@example.com")
    creator = await _hdr(client, "purge-owner@example.com")
    lib_a = await _create_active_standalone(client, creator, admin, name="A 在库")
    lib_b = await _create_active_standalone(client, creator, admin, name="B 垃圾桶")

    paper_id = await _new_paper(title="Purge from B only")
    await _add_membership(lib_a, paper_id, status="included", relevance=0.9)
    await _add_membership(lib_b, paper_id, status="excluded", relevance=0.2)

    resp = await client.delete(f"/api/libraries/{lib_b}/papers/{paper_id}", headers=creator)
    assert resp.status_code == 204, resp.text

    by_lib = await _statuses_by_lib(paper_id)
    assert str(lib_b) not in by_lib  # B 那份被彻底删除
    assert by_lib.get(str(lib_a)) == "included"  # A 那份未受影响
    async with get_sessionmaker()() as session:  # 内容池论文仍在（别的库复用）
        assert await session.get(Paper, uuid.UUID(paper_id)) is not None


async def test_scoped_restore_only_restores_current_library(client):
    """A 库在库、B 库垃圾桶(打过分)：从 B 库召回只把 B 那份→scored，A 那份不动。"""
    admin = await _hdr(client, "restore-admin@example.com")
    await _promote_admin("restore-admin@example.com")
    creator = await _hdr(client, "restore-owner@example.com")
    lib_a = await _create_active_standalone(client, creator, admin, name="A 在库2")
    lib_b = await _create_active_standalone(client, creator, admin, name="B 垃圾桶2")

    paper_id = await _new_paper(title="Restore in B only")
    await _add_membership(lib_a, paper_id, status="included", relevance=0.9)
    await _add_membership(lib_b, paper_id, status="excluded", relevance=0.3)

    resp = await client.post(
        f"/api/libraries/{lib_b}/papers/{paper_id}/restore", headers=creator
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "scored"  # 有分数无 wiki → 召回回 scored

    by_lib = await _statuses_by_lib(paper_id)
    assert by_lib.get(str(lib_b)) == "scored"  # B 那份被召回
    assert by_lib.get(str(lib_a)) == "included"  # A 那份未动


async def test_project_scoped_purge_hits_origin_library(client):
    """课题起源库(垃圾桶) + 另一库(在库)：DELETE /projects/{pid}/papers/{id} 只删起源库那份。"""
    admin = await _hdr(client, "projpurge-admin@example.com")
    await _promote_admin("projpurge-admin@example.com")
    creator = await _hdr(client, "projpurge-owner@example.com")
    pid, origin_lib = await make_project_with_library(client, creator, name="课题垃圾桶")
    other_lib = await _create_active_standalone(client, creator, admin, name="别的库2")

    paper_id = await _new_paper(title="Proj purge")
    await _add_membership(origin_lib, paper_id, status="excluded", relevance=0.2)
    await _add_membership(other_lib, paper_id, status="included", relevance=0.8)

    resp = await client.delete(f"/api/projects/{pid}/papers/{paper_id}", headers=creator)
    assert resp.status_code == 204, resp.text

    by_lib = await _statuses_by_lib(paper_id)
    assert str(origin_lib) not in by_lib  # 起源库那份被删
    assert by_lib.get(str(other_lib)) == "included"  # 别的库那份保留


async def test_scoped_purge_404_when_library_lacks_paper(client):
    admin = await _hdr(client, "purge404-admin@example.com")
    await _promote_admin("purge404-admin@example.com")
    creator = await _hdr(client, "purge404-owner@example.com")
    lib_a = await _create_active_standalone(client, creator, admin, name="A 库x")
    lib_b = await _create_active_standalone(client, creator, admin, name="B 库x")
    paper_id = await _new_paper(title="Only in A")
    await _add_membership(lib_a, paper_id, status="excluded", relevance=0.2)

    resp = await client.delete(f"/api/libraries/{lib_b}/papers/{paper_id}", headers=creator)
    assert resp.status_code == 404
    assert resp.json()["detail"] == "PAPER_NOT_FOUND"
