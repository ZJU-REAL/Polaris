"""库作用域引用导出（GET /libraries/{id}/export/citations，独立方向库也可用）。

覆盖：全库缺省导出（compiled/included）、按 ids 精确导出、excluded 垃圾桶与非成员不含、
attachment header 与文件名、非法 format→422。
"""

import json
import uuid

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


async def _create_active_standalone(client, creator_headers, admin_headers):
    resp = await client.post(
        "/api/libraries",
        json={"name": "引用导出库", "statement": "LLM agent 规划方向"},
        headers=creator_headers,
    )
    assert resp.status_code == 201, resp.text
    lib_id = resp.json()["id"]
    assert resp.json()["project_id"] is None
    resp = await client.post(f"/api/libraries/{lib_id}/approve", headers=admin_headers)
    assert resp.status_code == 200, resp.text
    return lib_id


async def _seed_member(lib_id, *, title, status, authors, year, venue=None, arxiv_id=None):
    async with get_sessionmaker()() as session:
        paper = Paper(
            title=title,
            authors=authors,
            year=year,
            venue=venue,
            arxiv_id=arxiv_id,
            source="arxiv",
        )
        session.add(paper)
        await session.flush()
        session.add(
            LibraryPaper(
                library_id=uuid.UUID(str(lib_id)),
                paper_id=paper.id,
                status=status,
            )
        )
        await session.commit()
        return str(paper.id)


async def _setup(client, prefix):
    admin = await _hdr(client, f"{prefix}-admin@example.com")
    await _promote_admin(f"{prefix}-admin@example.com")
    creator = await _hdr(client, f"{prefix}-owner@example.com")
    lib_id = await _create_active_standalone(client, creator, admin)
    p_conf = await _seed_member(
        lib_id,
        title="The Great Agent Benchmark",
        status="included",
        authors=[{"name": "Alice Smith"}],
        year=2024,
        venue="Proceedings of NeurIPS",
    )
    p_arxiv = await _seed_member(
        lib_id,
        title="Quantum Annealing Survey",
        status="compiled",
        authors=[{"name": "张三"}],
        year=2025,
        arxiv_id="2501.00042",
    )
    p_excluded = await _seed_member(
        lib_id,
        title="Excluded Paper",
        status="excluded",
        authors=[{"name": "Nobody"}],
        year=2020,
    )
    return creator, lib_id, p_conf, p_arxiv, p_excluded


async def test_library_bibtex_export_default_and_attachment(client):
    creator, lib_id, _p_conf, _p_arxiv, _p_excluded = await _setup(client, "libcite-bib")
    resp = await client.get(
        f"/api/libraries/{lib_id}/export/citations?format=bibtex", headers=creator
    )
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/plain")
    disp = resp.headers["content-disposition"]
    assert "attachment" in disp and "polaris-library-citations.bib" in disp
    bib = resp.text
    # 缺省导出在库成员（compiled/included），垃圾桶不含
    assert "@inproceedings{smith2024great,\n" in bib
    assert "@misc{张三2025quantum,\n" in bib
    assert "booktitle = {Proceedings of NeurIPS}," in bib
    assert "eprint = {2501.00042}," in bib
    assert "Excluded Paper" not in bib


async def test_library_export_ids_and_excluded_and_nonmember(client):
    creator, lib_id, p_conf, _p_arxiv, p_excluded = await _setup(client, "libcite-ids")

    # 按 ids 精确导出：只含选中那篇
    resp = await client.get(
        f"/api/libraries/{lib_id}/export/citations?format=bibtex&ids={p_conf}",
        headers=creator,
    )
    assert resp.status_code == 200, resp.text
    assert "The Great Agent Benchmark" in resp.text
    assert "Quantum Annealing Survey" not in resp.text

    # ids 命中 excluded（垃圾桶）成员 → 不含
    resp = await client.get(
        f"/api/libraries/{lib_id}/export/citations?format=bibtex&ids={p_excluded}",
        headers=creator,
    )
    assert resp.status_code == 200, resp.text
    assert resp.text.strip() == ""

    # ids 命中非成员论文（别的库/内容池）→ 不含
    stranger_id = str(uuid.uuid4())
    resp = await client.get(
        f"/api/libraries/{lib_id}/export/citations?format=csl-json&ids={stranger_id}",
        headers=creator,
    )
    assert resp.status_code == 200, resp.text
    assert json.loads(resp.text) == []


async def test_library_csl_json_and_invalid_format(client):
    creator, lib_id, _p_conf, _p_arxiv, _p_excluded = await _setup(client, "libcite-csl")
    resp = await client.get(
        f"/api/libraries/{lib_id}/export/citations?format=csl-json", headers=creator
    )
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("application/json")
    assert "polaris-library-citations.json" in resp.headers["content-disposition"]
    titles = {item["title"] for item in json.loads(resp.text)}
    assert titles == {"The Great Agent Benchmark", "Quantum Annealing Survey"}

    resp = await client.get(
        f"/api/libraries/{lib_id}/export/citations?format=ris", headers=creator
    )
    assert resp.status_code == 422
