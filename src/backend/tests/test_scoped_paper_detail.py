"""库/课题作用域的单篇详情读端点（fix：同一论文属多个库时详情读到错库那份成员行）。

覆盖：
- GET /libraries/{A}/papers/{id} 与 GET /libraries/{B}/papers/{id} 各返回本库那份
  relevance_score/status，不做跨库归并（对照无库作用域的 get_paper_for_user）。
- GET /projects/{pid}/papers/{id} 返回课题起源库那份成员行。
- 通过库作用域 batch-delete 删 A 库那份后，B 库那份仍在（作用域隔离）。
- 库不含该论文 → 404。
"""

import uuid

from sqlalchemy import select

from app.core.db import get_sessionmaker
from app.models.library_direction import LibraryPaper
from app.models.paper import Paper
from app.models.user import User
from tests.conftest import make_project_with_library, register_and_login


async def _hdr(client, email):
    return {"Authorization": f"Bearer {await register_and_login(client, email=email)}"}


async def _promote_admin(email: str) -> None:
    async with get_sessionmaker()() as session:
        user = (await session.execute(select(User).where(User.email == email))).scalar_one()
        user.role = "admin"
        await session.commit()


async def _create_active_standalone(client, creator_headers, admin_headers, name="独立库"):
    resp = await client.post(
        "/api/libraries",
        json={"name": name, "statement": "LLM agent 规划方向"},
        headers=creator_headers,
    )
    assert resp.status_code == 201, resp.text
    lib_id = resp.json()["id"]
    resp = await client.post(f"/api/libraries/{lib_id}/approve", headers=admin_headers)
    assert resp.status_code == 200, resp.text
    return lib_id


async def _new_paper(title="Shared paper") -> str:
    async with get_sessionmaker()() as session:
        paper = Paper(
            title=title,
            abstract="agent planning abstract",
            authors=[{"name": "Alice"}],
            year=2024,
            source="arxiv",
        )
        session.add(paper)
        await session.commit()
        return str(paper.id)


async def _add_membership(lib_id, paper_id, *, status, relevance, wiki=None) -> None:
    async with get_sessionmaker()() as session:
        session.add(
            LibraryPaper(
                library_id=uuid.UUID(str(lib_id)),
                paper_id=uuid.UUID(str(paper_id)),
                status=status,
                relevance_score=relevance,
                wiki_content=wiki,
            )
        )
        await session.commit()


async def test_library_scoped_detail_does_not_cross_libraries(client):
    """同一论文在 A(excluded,0.18) / B(included,0.96)：各库详情读到各自那份，不串。"""
    admin = await _hdr(client, "scoped-admin@example.com")
    await _promote_admin("scoped-admin@example.com")
    creator = await _hdr(client, "scoped-owner@example.com")
    lib_a = await _create_active_standalone(client, creator, admin, name="CUA 库")
    lib_b = await _create_active_standalone(client, creator, admin, name="Spatial Reasoning 库")

    paper_id = await _new_paper()
    await _add_membership(lib_a, paper_id, status="excluded", relevance=0.18)
    await _add_membership(lib_b, paper_id, status="included", relevance=0.96)

    resp = await client.get(f"/api/libraries/{lib_a}/papers/{paper_id}", headers=creator)
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "excluded"
    assert resp.json()["relevance_score"] == 0.18

    resp = await client.get(f"/api/libraries/{lib_b}/papers/{paper_id}", headers=creator)
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "included"
    assert resp.json()["relevance_score"] == 0.96


async def test_library_scoped_detail_404_when_not_member(client):
    admin = await _hdr(client, "scoped404-admin@example.com")
    await _promote_admin("scoped404-admin@example.com")
    creator = await _hdr(client, "scoped404-owner@example.com")
    lib_a = await _create_active_standalone(client, creator, admin, name="A 库")
    lib_b = await _create_active_standalone(client, creator, admin, name="B 库")

    paper_id = await _new_paper()
    await _add_membership(lib_a, paper_id, status="included", relevance=0.9)

    # 论文只在 A 库，问 B 库 → 404
    resp = await client.get(f"/api/libraries/{lib_b}/papers/{paper_id}", headers=creator)
    assert resp.status_code == 404
    assert resp.json()["detail"] == "PAPER_NOT_FOUND"
    # 完全不存在的论文 → 404
    resp = await client.get(f"/api/libraries/{lib_a}/papers/{uuid.uuid4()}", headers=creator)
    assert resp.status_code == 404


async def test_project_scoped_detail_reads_origin_library(client):
    """课题起源库(0.42) 与另一独立库(0.99) 各有一份：/projects/{pid}/papers 读起源库那份。"""
    admin = await _hdr(client, "scopedproj-admin@example.com")
    await _promote_admin("scopedproj-admin@example.com")
    creator = await _hdr(client, "scopedproj-owner@example.com")
    pid, origin_lib = await make_project_with_library(client, creator, name="课题起源库")
    other_lib = await _create_active_standalone(client, creator, admin, name="别的库")

    paper_id = await _new_paper(title="Cross-library paper")
    await _add_membership(origin_lib, paper_id, status="scored", relevance=0.42)
    await _add_membership(other_lib, paper_id, status="included", relevance=0.99)

    resp = await client.get(f"/api/projects/{pid}/papers/{paper_id}", headers=creator)
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "scored"
    assert resp.json()["relevance_score"] == 0.42

    # 不在该课题任何库的论文 → 404
    resp = await client.get(f"/api/projects/{pid}/papers/{uuid.uuid4()}", headers=creator)
    assert resp.status_code == 404


async def test_scoped_delete_isolates_other_library(client):
    """删 A 库那份后：A 库论文库空、A 详情 excluded；B 库那份仍 included、列表/详情可见。"""
    admin = await _hdr(client, "scopeddel-admin@example.com")
    await _promote_admin("scopeddel-admin@example.com")
    creator = await _hdr(client, "scopeddel-owner@example.com")
    lib_a = await _create_active_standalone(client, creator, admin, name="删除源库 A")
    lib_b = await _create_active_standalone(client, creator, admin, name="保留库 B")

    paper_id = await _new_paper(title="Delete-me-in-A only")
    await _add_membership(lib_a, paper_id, status="included", relevance=0.5)
    await _add_membership(lib_b, paper_id, status="included", relevance=0.7)

    # 库作用域批删（单篇）A 库那份
    resp = await client.post(
        f"/api/libraries/{lib_a}/papers/batch-delete",
        json={"paper_ids": [paper_id]},
        headers=creator,
    )
    assert resp.status_code == 200 and resp.json()["deleted"] == 1

    # A 库：论文库不再有它，详情读到 excluded 那份
    resp = await client.get(f"/api/libraries/{lib_a}/papers?status=library", headers=creator)
    assert resp.json()["total"] == 0
    resp = await client.get(f"/api/libraries/{lib_a}/papers/{paper_id}", headers=creator)
    assert resp.status_code == 200 and resp.json()["status"] == "excluded"

    # B 库：不受影响，列表可见、详情仍 included
    resp = await client.get(f"/api/libraries/{lib_b}/papers?status=library", headers=creator)
    assert [p["id"] for p in resp.json()["items"]] == [paper_id]
    resp = await client.get(f"/api/libraries/{lib_b}/papers/{paper_id}", headers=creator)
    assert resp.status_code == 200 and resp.json()["status"] == "included"
    assert resp.json()["relevance_score"] == 0.7
