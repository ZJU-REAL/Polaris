"""解读正文里不再夹带证据定位 JSON（#873）。

图文编译曾把定位信息作为 ``<!-- polaris-ai-evidence:{json} -->`` 追加在解读正文末尾。
渲染器把注释当文字显示（「把文章添加到相关研究里面后面会出现一段乱码」），导出文件
和喂给模型的「论文解读」里也都带着它。现在定位信息单独存、单独给。
"""

import io
import json
import uuid
import zipfile

import pytest

from app.core.db import get_sessionmaker
from app.models.paper import PaperWiki
from app.services.paper_wiki import content_for, upsert_wiki
from tests.conftest import add_paper, make_project_with_library, owner_of, register_and_login

MARKER = "polaris-ai-evidence"
REFS = [
    {
        "article_no": 1,
        "sentence_no": 31,
        "paper_id": "11111111-1111-1111-1111-111111111111",
        "anchor_id": "22222222-2222-2222-2222-222222222222",
        "href": "/papers/11111111-1111-1111-1111-111111111111/read?evidence=2222",
        "parser": "pymupdf",
    }
]
EVIDENCE = {"version": 1, "mode": "fulltext", "paper_count": 1, "refs": REFS}
BODY = "## TL;DR\n\n一句话总结。\n\n## 结果\n\n有依据的结论 [文1·句31]。"
# 旧版本落库的样子：正文 + 末尾一段注释
LEGACY = (
    f"{BODY}\n\n<!-- {MARKER}:"
    f"{json.dumps(EVIDENCE, ensure_ascii=False, separators=(',', ':'))} -->\n"
)


def _assert_clean(text: str | None) -> None:
    assert text, "expected a summary"
    assert MARKER not in text
    assert "<!--" not in text
    assert "anchor_id" not in text and "pymupdf" not in text
    assert "有依据的结论 [文1·句31]" in text


def test_split_evidence_manifest_separates_text_and_locators():
    from app.core.wiki_evidence import split_evidence_manifest

    assert split_evidence_manifest(LEGACY) == (BODY, EVIDENCE)
    # 没有注释：原样返回，一个字符都不动
    plain = "正文\n\n<!-- 普通注释 -->\n"
    assert split_evidence_manifest(plain) == (plain, None)
    assert split_evidence_manifest(None) == ("", None)
    # 注释后面又被接了内容（例如导出文件被编辑后重新导入）：照样拆掉
    body, evidence = split_evidence_manifest(LEGACY + "\n## 我的笔记\n\n补充。\n")
    assert MARKER not in body and body.endswith("补充。") and body.startswith(BODY)
    assert evidence == EVIDENCE
    # JSON 坏了也不留给用户看
    body, evidence = split_evidence_manifest(f"正文\n\n<!-- {MARKER}:{{oops}} -->\n")
    assert (body, evidence) == ("正文", None)


async def test_stored_summary_never_contains_the_evidence_comment(client):
    """任何写路径送进来旧格式，落库的正文都是干净的，定位信息进 evidence 列。"""
    headers = {"Authorization": f"Bearer {await register_and_login(client)}"}
    project_id, _ = await make_project_with_library(client, headers, name="store")
    async with get_sessionmaker()() as session:
        direct = await add_paper(
            session, project_id=project_id, title="Direct", wiki_content=LEGACY
        )
        via_upsert = await add_paper(session, project_id=project_id, title="Upsert")
        await upsert_wiki(session, paper=via_upsert, content=LEGACY)
        fresh = await add_paper(session, project_id=project_id, title="Fresh")
        await upsert_wiki(session, paper=fresh, content=BODY, evidence=EVIDENCE)
        await session.commit()
        ids = [direct.id, via_upsert.id, fresh.id]

    async with get_sessionmaker()() as session:
        for paper_id in ids:
            _assert_clean(await content_for(session, paper_id))
            wiki = (
                await session.execute(
                    PaperWiki.__table__.select().where(PaperWiki.paper_id == paper_id)
                )
            ).one()
            assert wiki.content == BODY
            assert wiki.evidence == EVIDENCE
        # 重新编译没有证据 → 旧的定位信息不留
        paper = await session.get(type(direct), ids[0])
        await upsert_wiki(session, paper=paper, content="新正文")
        await session.commit()
    async with get_sessionmaker()() as session:
        paper = await session.get(type(direct), ids[0])
        assert paper.wiki_content == "新正文"
        assert paper.wiki_evidence is None


async def test_every_endpoint_returns_clean_summary_and_reader_gets_evidence(client, monkeypatch):
    """库详情 / 课题视角 / 相关研究 / 个人库 / 每日推送：正文都不含注释；
    论文详情（解读页与论文列表详情用它渲染可点的引用）另给 wiki_evidence。"""
    headers = {"Authorization": f"Bearer {await register_and_login(client)}"}
    project_id, library_id = await make_project_with_library(client, headers, name="views")
    async with get_sessionmaker()() as session:
        paper = await add_paper(
            session,
            project_id=project_id,
            title="Evidence Paper",
            arxiv_id="2401.00873",
            status="compiled",
            wiki_content=LEGACY,
        )
        await session.commit()
        paper_id = str(paper.id)

    # 论文详情三条路径
    for url in (
        f"/api/papers/{paper_id}",
        f"/api/projects/{project_id}/papers/{paper_id}",
        f"/api/libraries/{library_id}/papers/{paper_id}",
    ):
        resp = await client.get(url, headers=headers)
        assert resp.status_code == 200, (url, resp.text)
        _assert_clean(resp.json()["wiki_content"])
        assert resp.json()["wiki_content"] == BODY
        assert resp.json()["wiki_evidence"] == EVIDENCE, url

    # 相关研究（书架）——用户报的就是这里
    resp = await client.post(
        f"/api/projects/{project_id}/shelf", json={"paper_id": paper_id}, headers=headers
    )
    assert resp.status_code == 201, resp.text
    _assert_clean(resp.json()["wiki_content"])
    resp = await client.get(f"/api/projects/{project_id}/shelf", headers=headers)
    _assert_clean(resp.json()["items"][0]["wiki_content"])

    # 个人库详情（入架必入个人库）
    entries = (await client.get("/api/me/library?tab=saved", headers=headers)).json()["items"]
    resp = await client.get(f"/api/me/library/{entries[0]['id']}", headers=headers)
    assert resp.status_code == 200, resp.text
    _assert_clean(resp.json()["wiki_content"])

    # 每日推送详情
    from app.services import daily_feed
    from tests.test_daily_feed import _rss_entry, _StubArxiv

    monkeypatch.setattr(
        daily_feed,
        "get_arxiv_client",
        lambda: _StubArxiv({"cs.AI": [_rss_entry("2607.00873", "D")]}),
    )
    async with get_sessionmaker()() as session:
        await daily_feed.set_categories(session, ["cs.AI"], user=await owner_of(session))
        await daily_feed.sync_daily_feed(session)
    item = (await client.get("/api/daily/papers", headers=headers)).json()["items"][0]
    async with get_sessionmaker()() as session:
        session.add(PaperWiki(paper_id=uuid.UUID(item["paper_id"]), content=LEGACY))
        await session.commit()
    resp = await client.get(f"/api/daily/papers/{item['entry_id']}", headers=headers)
    assert resp.status_code == 200, resp.text
    _assert_clean(resp.json()["wiki_content"])


async def test_export_and_model_context_have_no_evidence_comment(client):
    headers = {"Authorization": f"Bearer {await register_and_login(client)}"}
    project_id, _ = await make_project_with_library(client, headers, name="ctx")
    async with get_sessionmaker()() as session:
        paper = await add_paper(
            session,
            project_id=project_id,
            title="Context Paper",
            status="compiled",
            wiki_content=LEGACY,
        )
        await session.commit()
        paper_id = paper.id

    # Obsidian 导出
    resp = await client.get(f"/api/projects/{project_id}/export/obsidian", headers=headers)
    assert resp.status_code == 200, resp.text
    with zipfile.ZipFile(io.BytesIO(resp.content)) as archive:
        pages = {
            name: archive.read(name).decode("utf-8")
            for name in archive.namelist()
            if name.endswith(".md")
        }
    exported = [text for text in pages.values() if "有依据的结论" in text]
    assert exported, sorted(pages)
    for text in pages.values():
        assert MARKER not in text and "anchor_id" not in text

    # 给模型的上下文：智能体工具「读解读」、伴读兜底上下文
    import app.tools as tools
    from app.services.papers import build_chat_context, get_paper_for_user
    from app.tools.context import ToolContext

    async with get_sessionmaker()() as session:
        owner = await owner_of(session)
        ctx = ToolContext(project_id=uuid.UUID(project_id), llm=None, user_id=owner.id)
        view = await get_paper_for_user(session, paper_id=paper_id, user_id=owner.id)
        _assert_clean(build_chat_context(view))
    result = await tools.run_tool(ctx, "read_wiki", {"paper_id": str(paper_id)})
    _assert_clean(result["wiki"])
    assert MARKER not in json.dumps(result, ensure_ascii=False)


@pytest.mark.parametrize("needle", ["pymupdf", "anchor_id", "sentence_no"])
async def test_search_does_not_match_evidence_json(client, needle):
    """正文搜索搜的是正文：定位 JSON 里的键名、解析器名不该让每篇论文都命中。"""
    headers = {"Authorization": f"Bearer {await register_and_login(client)}"}
    project_id, _ = await make_project_with_library(client, headers, name="search")
    async with get_sessionmaker()() as session:
        await add_paper(
            session,
            project_id=project_id,
            title="Searchable",
            status="compiled",
            wiki_content=LEGACY,
        )
        await session.commit()
    async with get_sessionmaker()() as session:
        from sqlalchemy import select

        hits = (
            await session.execute(
                select(PaperWiki.paper_id).where(PaperWiki.content.ilike(f"%{needle}%"))
            )
        ).all()
        assert hits == []
