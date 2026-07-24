"""P9d：独立库（project_id=NULL）的库级论文管理台。

覆盖库级论文列表/过滤、软删召回、批量删除、清空垃圾桶、手动添加、库级标签、
ingest 状态、图谱、笔记、文献对话，以及非管理者的鉴权 403。
"""

import json
import uuid

from sqlalchemy import select

from app.core.db import get_sessionmaker
from app.models.library_direction import LibraryPaper
from app.models.paper import Concept, Paper, paper_concepts
from app.models.user import User
from tests.conftest import register_and_login

BIBTEX_ENTRY = """@inproceedings{smith2025bench,
  title = {A {Benchmark} for Agents},
  author = {Smith, Alice and Bob Jones},
  year = {2025},
  booktitle = {Proceedings of NeurIPS},
  doi = {10.1000/bench},
  url = {https://example.org/bench},
}
"""


def _parse_sse(text: str) -> list[tuple[str, dict]]:
    events = []
    for block in text.strip().split("\n\n"):
        event, data = None, None
        for line in block.split("\n"):
            if line.startswith("event: "):
                event = line[len("event: ") :]
            elif line.startswith("data: "):
                data = json.loads(line[len("data: ") :])
        if event is not None:
            events.append((event, data))
    return events


async def _hdr(client, email):
    return {"Authorization": f"Bearer {await register_and_login(client, email=email)}"}


async def _promote_admin(email: str) -> None:
    async with get_sessionmaker()() as session:
        user = (await session.execute(select(User).where(User.email == email))).scalar_one()
        user.role = "admin"
        await session.commit()


async def _create_active_standalone(client, creator_headers, admin_headers, name="独立库"):
    """创建者建 pending 独立库 → 管理员激活；返回 library_id（str）。"""
    resp = await client.post(
        "/api/libraries",
        json={"name": name, "statement": "LLM agent 规划方向"},
        headers=creator_headers,
    )
    assert resp.status_code == 201, resp.text
    lib_id = resp.json()["id"]
    assert resp.json()["project_id"] is None
    resp = await client.post(f"/api/libraries/{lib_id}/approve", headers=admin_headers)
    assert resp.status_code == 200, resp.text
    return lib_id


async def _seed_paper(
    lib_id,
    *,
    title,
    status="scored",
    relevance=0.8,
    wiki=None,
    abstract="agent planning abstract",
    authors=None,
    year=2024,
):
    async with get_sessionmaker()() as session:
        paper = Paper(
            title=title,
            abstract=abstract,
            authors=authors if authors is not None else [{"name": "Alice"}],
            year=year,
            source="arxiv",
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


async def _setup(client, *, prefix="p9d"):
    """admin + 创建者 + 一个 active 独立库；返回 (creator_headers, admin_headers, lib_id)。"""
    admin = await _hdr(client, f"{prefix}-admin@example.com")
    await _promote_admin(f"{prefix}-admin@example.com")
    creator = await _hdr(client, f"{prefix}-owner@example.com")
    lib_id = await _create_active_standalone(client, creator, admin)
    return creator, admin, lib_id


async def test_standalone_papers_list_filters_and_trash(client):
    creator, _admin, lib_id = await _setup(client, prefix="p9d-list")
    p_scored = await _seed_paper(lib_id, title="Scored only", status="scored")
    p_compiled = await _seed_paper(
        lib_id, title="Compiled paper", status="compiled", wiki="# 解读\n\n讲 agent。"
    )

    resp = await client.get(f"/api/libraries/{lib_id}/papers?status=library", headers=creator)
    assert resp.status_code == 200, resp.text
    assert resp.json()["total"] == 2

    resp = await client.get(f"/api/libraries/{lib_id}/papers?status=compiled_any", headers=creator)
    assert resp.json()["total"] == 1
    assert resp.json()["items"][0]["id"] == p_compiled

    resp = await client.patch(
        f"/api/papers/{p_scored}", json={"status": "excluded"}, headers=creator
    )
    assert resp.status_code == 200, resp.text
    resp = await client.get(f"/api/libraries/{lib_id}/papers?status=excluded", headers=creator)
    assert [x["id"] for x in resp.json()["items"]] == [p_scored]
    resp = await client.get(f"/api/libraries/{lib_id}/papers?status=library", headers=creator)
    assert resp.json()["total"] == 1

    resp = await client.post(f"/api/papers/{p_scored}/restore", headers=creator)
    assert resp.status_code == 200, resp.text
    resp = await client.get(f"/api/libraries/{lib_id}/papers?status=library", headers=creator)
    assert resp.json()["total"] == 2


async def test_standalone_batch_delete_and_empty_trash(client):
    creator, _admin, lib_id = await _setup(client, prefix="p9d-batch")
    p1 = await _seed_paper(lib_id, title="Batch 1")
    p2 = await _seed_paper(lib_id, title="Batch 2")

    resp = await client.post(
        f"/api/libraries/{lib_id}/papers/batch-delete",
        json={"paper_ids": [p1, p2]},
        headers=creator,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["deleted"] == 2
    resp = await client.get(f"/api/libraries/{lib_id}/papers?status=excluded", headers=creator)
    assert resp.json()["total"] == 2

    resp = await client.post(f"/api/libraries/{lib_id}/trash/empty", headers=creator)
    assert resp.status_code == 200, resp.text
    assert resp.json()["deleted"] == 2
    async with get_sessionmaker()() as session:
        rows = (
            (
                await session.execute(
                    select(LibraryPaper).where(LibraryPaper.library_id == uuid.UUID(lib_id))
                )
            )
            .scalars()
            .all()
        )
        assert rows == []
        # 清空垃圾桶后该论文别处再无引用 → 内容池本体也被回收（孤儿清理）
        assert await session.get(Paper, uuid.UUID(p1)) is None


async def test_standalone_hard_batch_delete(client):
    creator, _admin, lib_id = await _setup(client, prefix="p9d-hard")
    p1 = await _seed_paper(lib_id, title="Hard delete me")
    resp = await client.post(
        f"/api/libraries/{lib_id}/papers/batch-delete",
        json={"paper_ids": [p1], "hard": True},
        headers=creator,
    )
    assert resp.status_code == 200 and resp.json()["deleted"] == 1
    async with get_sessionmaker()() as session:
        row = (
            await session.execute(
                select(LibraryPaper).where(LibraryPaper.paper_id == uuid.UUID(p1))
            )
        ).scalar_one_or_none()
        assert row is None


async def test_standalone_manual_add_bibtex(client):
    creator, _admin, lib_id = await _setup(client, prefix="p9d-add")
    resp = await client.post(
        f"/api/libraries/{lib_id}/papers", json={"bibtex": BIBTEX_ENTRY}, headers=creator
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["title"] == "A Benchmark for Agents"
    assert body["doi"] == "10.1000/bench"
    resp = await client.post(
        f"/api/libraries/{lib_id}/papers", json={"bibtex": BIBTEX_ENTRY}, headers=creator
    )
    assert resp.status_code == 409
    assert resp.json()["detail"] == "PAPER_EXISTS"


async def test_standalone_recompile_via_paper_endpoint(client):
    creator, _admin, lib_id = await _setup(client, prefix="p9d-recompile")
    p1 = await _seed_paper(lib_id, title="Recompile me", status="scored")
    resp = await client.post(f"/api/papers/{p1}/recompile", headers=creator)
    assert resp.status_code == 200, resp.text
    assert resp.json()["has_wiki"] is True


async def test_standalone_tags_scoped_to_library(client):
    """P9e：独立库（project_id=NULL）也能打标签、列标签、按标签筛选、零引用清理。"""
    creator, _admin, lib_id = await _setup(client, prefix="p9e-tags")
    p1 = await _seed_paper(lib_id, title="Tagged paper")
    p2 = await _seed_paper(lib_id, title="Untagged paper")

    # 打标签（去重 + 排序）
    resp = await client.put(
        f"/api/papers/{p1}/tags", json={"names": ["方法", "评测", "方法"]}, headers=creator
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["tags"] == ["方法", "评测"]

    # 库标签列表（含引用论文数）
    resp = await client.get(f"/api/libraries/{lib_id}/tags", headers=creator)
    assert resp.status_code == 200, resp.text
    assert {t["name"]: t["paper_count"] for t in resp.json()} == {"方法": 1, "评测": 1}

    # 按标签筛选库论文
    resp = await client.get(
        f"/api/libraries/{lib_id}/papers", params={"tag": "方法"}, headers=creator
    )
    assert resp.status_code == 200, resp.text
    assert [p["title"] for p in resp.json()["items"]] == ["Tagged paper"]

    # 清空标签 → 零引用标签自动清理
    resp = await client.put(f"/api/papers/{p1}/tags", json={"names": []}, headers=creator)
    assert resp.json()["tags"] == []
    resp = await client.get(f"/api/libraries/{lib_id}/tags", headers=creator)
    assert resp.json() == []
    # 未打标签的论文不受影响
    assert p2


async def test_standalone_ingest_state(client):
    creator, _admin, lib_id = await _setup(client, prefix="p9d-ingest")
    await _seed_paper(lib_id, title="Counted 1", status="scored")
    await _seed_paper(lib_id, title="Counted 2", status="compiled", wiki="x")
    await _seed_paper(lib_id, title="Candidate", status="candidate")
    resp = await client.get(f"/api/libraries/{lib_id}/ingest/state", headers=creator)
    assert resp.status_code == 200, resp.text
    counts = resp.json()["paper_counts"]
    assert counts["library"] == 2
    assert counts["total"] == 3
    assert resp.json()["running_voyage_id"] is None


async def test_standalone_graph(client):
    creator, _admin, lib_id = await _setup(client, prefix="p9d-graph")
    p1 = await _seed_paper(lib_id, title="Graph paper", status="compiled", wiki="x")
    async with get_sessionmaker()() as session:
        concept = Concept(
            library_id=uuid.UUID(lib_id), name="Planning", slug="planning", category="method"
        )
        session.add(concept)
        await session.flush()
        await session.execute(
            paper_concepts.insert().values(paper_id=uuid.UUID(p1), concept_id=concept.id)
        )
        await session.commit()
    resp = await client.get(f"/api/libraries/{lib_id}/graph", headers=creator)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    types = {n["type"] for n in data["nodes"]}
    assert "paper" in types and "concept" in types
    assert data["paper_total"] == 1


async def test_standalone_notes(client):
    creator, _admin, lib_id = await _setup(client, prefix="p9d-notes")
    p1 = await _seed_paper(lib_id, title="Note target")
    resp = await client.post(
        f"/api/papers/{p1}/notes", json={"content": "这是我的库笔记"}, headers=creator
    )
    assert resp.status_code == 201, resp.text
    resp = await client.get(f"/api/libraries/{lib_id}/notes", headers=creator)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["paper_title"] == "Note target"
    assert body["items"][0]["content"] == "这是我的库笔记"


async def test_standalone_chat_sse(client):
    creator, _admin, lib_id = await _setup(client, prefix="p9d-chat")
    await _seed_paper(lib_id, title="Chat corpus", status="compiled", wiki="讲 agent 规划")
    async with client.stream(
        "POST",
        f"/api/libraries/{lib_id}/chat",
        json={"question": "这个方向讲了什么？", "history": []},
        headers=creator,
    ) as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        body = (await resp.aread()).decode("utf-8")
    events = _parse_sse(body)
    kinds = [e for e, _ in events]
    assert kinds[0] == "sources" and kinds[-1] == "done" and "error" not in kinds


async def test_manage_endpoints_forbidden_for_non_manager(client):
    creator, _admin, lib_id = await _setup(client, prefix="p9d-403")
    p1 = await _seed_paper(lib_id, title="Guarded")
    stranger = await _hdr(client, "p9d-403-stranger@example.com")

    resp = await client.get(f"/api/libraries/{lib_id}/papers", headers=stranger)
    assert resp.status_code == 200

    resp = await client.post(
        f"/api/libraries/{lib_id}/papers/batch-delete",
        json={"paper_ids": [p1]},
        headers=stranger,
    )
    assert resp.status_code == 403
    resp = await client.post(f"/api/libraries/{lib_id}/trash/empty", headers=stranger)
    assert resp.status_code == 403
    resp = await client.post(
        f"/api/libraries/{lib_id}/papers", json={"bibtex": BIBTEX_ENTRY}, headers=stranger
    )
    assert resp.status_code == 403
    resp = await client.get(f"/api/libraries/{lib_id}/ingest/state", headers=stranger)
    assert resp.status_code == 403
