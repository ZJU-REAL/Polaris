"""课题相关研究 / 个人库文献对话（scope=论文集合）：检索纯 paper_ids 分支 + 两个 SSE 端点。"""

import json
import uuid
from pathlib import Path

from sqlalchemy import select

from app.core.db import get_sessionmaker
from app.models.paper import Paper, PaperChunk
from app.models.user import User
from tests.conftest import add_paper, register_and_login


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


async def _user_id(email: str) -> uuid.UUID:
    async with get_sessionmaker()() as session:
        return (
            await session.execute(select(User.id).where(User.email == email))
        ).scalar_one()


# ---- personal_paper_ids（纯业务） ----


async def test_personal_paper_ids_filters_saved_and_soft_ref(client):
    """只返回「saved 且 last_paper_id 非空」的条目，其余（未收藏 / 软引用为空）剔除。"""
    email = "scoped-personal@example.com"
    await register_and_login(client, email=email)
    uid = await _user_id(email)

    from app.models.library import UserLibraryEntry
    from app.services.user_library import personal_paper_ids

    async with get_sessionmaker()() as session:
        # 三篇真实内容池论文（last_paper_id 是指向 papers.id 的软引用外键）
        p_kept, p_unsaved = Paper(title="Kept"), Paper(title="Unsaved")
        session.add_all([p_kept, p_unsaved])
        await session.flush()
        kept = p_kept.id
        # saved + 有软引用 → 保留
        session.add(
            UserLibraryEntry(
                user_id=uid, dedup_key="k-kept", title="Kept", saved=True, last_paper_id=kept
            )
        )
        # saved 但软引用为空 → 剔除
        session.add(
            UserLibraryEntry(
                user_id=uid, dedup_key="k-null", title="NullRef", saved=True, last_paper_id=None
            )
        )
        # 有软引用但未收藏 → tab=saved 剔除
        session.add(
            UserLibraryEntry(
                user_id=uid,
                dedup_key="k-unsaved",
                title="Unsaved",
                saved=False,
                last_paper_id=p_unsaved.id,
            )
        )
        await session.commit()

        saved_ids = await personal_paper_ids(session, user_id=uid, tab="saved")
        assert saved_ids == [kept]
        # tab=history（非 saved）：收录所有有软引用的条目，含未收藏
        any_ids = await personal_paper_ids(session, user_id=uid, tab="history")
        assert kept in any_ids and len(any_ids) == 2


# ---- 纯 paper_ids 检索分支：不 join library_papers ----


async def test_keyword_search_pure_paper_ids_no_library_join(client):
    """library_ids=None + paper_ids 时按论文集合直接检索，不依赖 library_papers 成员行。"""
    from app.services.chunks import keyword_search_chunks

    async with get_sessionmaker()() as session:
        # 直接建内容池论文 + 分段，不建任何 library 成员行
        paper = Paper(title="Orphan Planning Paper")
        session.add(paper)
        await session.flush()
        session.add(PaperChunk(paper_id=paper.id, seq=0, text="planning tree search 规划方法"))
        session.add(PaperChunk(paper_id=paper.id, seq=1, text="无关内容 unrelated"))
        await session.commit()
        pid = paper.id

        hits = await keyword_search_chunks(
            session, library_ids=None, q="planning search", limit=10, paper_ids=[pid]
        )
        assert hits, "纯 paper_ids 分支应命中孤儿论文（无 membership）的分段"
        assert all(c.paper_id == pid for c, _ in hits)

        # 换一个不存在的 paper_id → 空
        empty = await keyword_search_chunks(
            session, library_ids=None, q="planning", limit=10, paper_ids=[uuid.uuid4()]
        )
        assert empty == []


# ---- 两个 SSE 端点 ----


async def _project_with_papers(client, headers):
    resp = await client.post(
        "/api/projects",
        json={"name": "scoped-chat", "definition": {"statement": "LLM agent 规划"}},
        headers=headers,
    )
    project_id = uuid.UUID(resp.json()["id"])
    txt_dir = Path(__import__("tempfile").mkdtemp(prefix="polaris-scoped-"))
    async with get_sessionmaker()() as session:
        ids = []
        seeds = [
            ("Planning with Tree Search", "planning agent tree search " + "规划方法细节。" * 200),
            ("Reflexion Self-Improvement", "reflexion self improve " + "自我反思细节。" * 200),
        ]
        for i, (title, body) in enumerate(seeds):
            txt = txt_dir / f"p{i}.txt"
            txt.write_text(body, encoding="utf-8")
            p = await add_paper(
                session,
                project_id=project_id,
                title=title,
                abstract=f"{title} abstract",
                tldr=f"{title} 的一句话总结",
                year=2025,
                relevance_score=0.9 - i * 0.1,
                status="compiled",
                full_text_path=str(txt),
            )
            ids.append(str(p.id))
        await session.commit()
    return project_id, ids


async def test_shelf_chat_sse(client):
    """课题相关研究对话：书架论文集合 → sources + delta/done。"""
    token = await register_and_login(client, email="scoped-shelf@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    project_id, ids = await _project_with_papers(client, headers)

    # 入架两篇 + 建全文索引
    for pid in ids:
        resp = await client.post(
            f"/api/projects/{project_id}/shelf", json={"paper_id": pid}, headers=headers
        )
        assert resp.status_code == 201, resp.text
    await client.post(f"/api/projects/{project_id}/index/rebuild", headers=headers)

    async with client.stream(
        "POST",
        f"/api/projects/{project_id}/shelf/chat",
        json={"question": "planning 方向的主流方法有哪些？", "history": []},
        headers=headers,
    ) as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        body = (await resp.aread()).decode("utf-8")

    events = _parse_sse(body)
    kinds = [e for e, _ in events]
    assert kinds[0] == "sources" and kinds[-1] == "done" and "error" not in kinds
    items = events[0][1]["items"]
    assert len(items) >= 1
    assert {"index", "paper_id", "title", "year"} <= set(items[0])
    # 无 membership → status/relevance 为 None
    assert items[0]["status"] is None and items[0]["relevance"] is None
    answer = "".join(d["text"] for e, d in events if e == "delta")
    assert "fake 文献综合" in answer

    # 非成员 404
    other = await register_and_login(client, email="scoped-outsider@example.com")
    resp = await client.post(
        f"/api/projects/{project_id}/shelf/chat",
        json={"question": "hi"},
        headers={"Authorization": f"Bearer {other}"},
    )
    assert resp.status_code == 404


async def test_shelf_chat_covers_linked_library_corpus(client):
    """回归：课题对话语料 = 关联文献库 ∪ 相关研究书架。

    论文只在课题关联库里、没入书架时，课题对话仍应感知到它们
    （修复前 scope 只有 shelf_paper_ids，空书架 → 感知不到关联库文献）。"""
    token = await register_and_login(client, email="scoped-corpus@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    project_id, _ids = await _project_with_papers(client, headers)
    # 关键：不入书架（shelf 为空），论文只在课题关联库里

    async with client.stream(
        "POST",
        f"/api/projects/{project_id}/shelf/chat",
        json={"question": "planning 方向有哪些方法？", "history": []},
        headers=headers,
    ) as resp:
        assert resp.status_code == 200
        body = (await resp.aread()).decode("utf-8")

    events = _parse_sse(body)
    kinds = [e for e, _ in events]
    assert kinds[0] == "sources" and kinds[-1] == "done" and "error" not in kinds
    items = events[0][1]["items"]
    assert len(items) >= 1, "关联库里的论文应进入课题对话语料，即使没入书架"
    titles = {it["title"] for it in items}
    assert titles & {"Planning with Tree Search", "Reflexion Self-Improvement"}


async def test_shelf_chat_empty_corpus(client):
    """空书架 + 无关联库：无论文 → sources 空、仍能 done 收尾（走空语料分支）。"""
    token = await register_and_login(client, email="scoped-empty-shelf@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    resp = await client.post(
        "/api/projects", json={"name": "empty-shelf"}, headers=headers
    )
    project_id = resp.json()["id"]

    async with client.stream(
        "POST",
        f"/api/projects/{project_id}/shelf/chat",
        json={"question": "有哪些论文？", "history": []},
        headers=headers,
    ) as resp:
        assert resp.status_code == 200
        body = (await resp.aread()).decode("utf-8")
    events = _parse_sse(body)
    kinds = [e for e, _ in events]
    assert kinds[0] == "sources" and kinds[-1] == "done" and "error" not in kinds
    assert events[0][1]["items"] == []


async def test_personal_library_chat_sse(client):
    """个人库对话：本人收藏论文集合 → sources + delta/done（无 project 成员校验）。"""
    token = await register_and_login(client, email="scoped-lib@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    project_id, ids = await _project_with_papers(client, headers)

    # 收藏一篇到个人库 + 建索引（个人库对话复用现有已索引 chunk）
    resp = await client.post(
        "/api/me/library", json={"paper_id": ids[0]}, headers=headers
    )
    assert resp.status_code == 201, resp.text
    await client.post(f"/api/projects/{project_id}/index/rebuild", headers=headers)

    async with client.stream(
        "POST",
        "/api/library/chat",
        json={"question": "planning tree search 有什么方法？", "history": []},
        headers=headers,
    ) as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        body = (await resp.aread()).decode("utf-8")

    events = _parse_sse(body)
    kinds = [e for e, _ in events]
    assert kinds[0] == "sources" and kinds[-1] == "done" and "error" not in kinds
    items = events[0][1]["items"]
    assert len(items) == 1 and items[0]["paper_id"] == ids[0]
    assert items[0]["status"] is None and items[0]["relevance"] is None
    answer = "".join(d["text"] for e, d in events if e == "delta")
    assert "fake 文献综合" in answer
