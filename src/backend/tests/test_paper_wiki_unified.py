"""论文解读统一成每篇一份（paper_wikis）+ 概念统一成论文级。

覆盖：编译只落唯一一行、库/每日推送/个人库三条路径读到同一份、重编译覆盖并换编译者、
概念按「库内论文关联」推导作用域、概念详情可选库作用域。LLM 走 fake provider（conftest）。
"""

import uuid

from sqlalchemy import func, select

from app.core.db import get_sessionmaker
from app.models.paper import Concept, PaperWiki, paper_concepts
from tests.conftest import add_paper, make_project_with_library, register_and_login, wiki_of


async def _setup(client, *, name="unified-proj", email="wiki-unified@example.com"):
    token = await register_and_login(client, email=email)
    headers = {"Authorization": f"Bearer {token}"}
    project_id, library_id = await make_project_with_library(client, headers, name=name)
    return project_id, library_id, headers


async def _wiki_row_count(paper_id: str) -> int:
    async with get_sessionmaker()() as session:
        stmt = select(func.count()).where(PaperWiki.paper_id == uuid.UUID(paper_id))
        return int((await session.execute(stmt)).scalar_one())


# ---- 编译落唯一一行 ----


async def test_recompile_writes_single_wiki_row_and_records_compiler(client):
    project_id, _library_id, headers = await _setup(client)
    async with get_sessionmaker()() as session:
        paper = await add_paper(
            session,
            project_id=uuid.UUID(project_id),
            title="Unified Wiki Paper",
            abstract="Everything about agents.",
            status="scored",
        )
        await session.commit()
        paper_id = str(paper.id)

    resp = await client.post(f"/api/papers/{paper_id}/recompile", headers=headers)
    assert resp.status_code == 200, resp.text
    detail = resp.json()
    assert detail["has_wiki"] is True and detail["wiki_content"].strip()
    assert detail["compiled_model"] == "fake-default"
    assert detail["compiled_by_name"]  # 编译者随详情带出（重编译前的覆盖提示用）
    assert await _wiki_row_count(paper_id) == 1

    # 再编译一次：仍然只有一行，内容/时间以最新为准
    first = await wiki_of_str(paper_id)
    resp = await client.post(f"/api/papers/{paper_id}/recompile", headers=headers)
    assert resp.status_code == 200, resp.text
    assert await _wiki_row_count(paper_id) == 1
    second = await wiki_of_str(paper_id)
    assert second.updated_at >= first.updated_at


async def wiki_of_str(paper_id: str) -> PaperWiki:
    async with get_sessionmaker()() as session:
        wiki = await wiki_of(session, paper_id=paper_id)
        assert wiki is not None
        return wiki


async def test_second_user_recompile_overwrites_and_takes_over_compiled_by(client):
    """谁都能重编、以最新为准：内容被覆盖，compiled_by 换成最后编译的人。"""
    project_id, _library_id, headers = await _setup(
        client, name="overwrite-proj", email="wiki-owner@example.com"
    )
    async with get_sessionmaker()() as session:
        paper = await add_paper(
            session,
            project_id=uuid.UUID(project_id),
            title="Overwritten Paper",
            abstract="abstract",
            status="scored",
        )
        await session.commit()
        paper_id = str(paper.id)

    await client.post(f"/api/papers/{paper_id}/recompile", headers=headers)
    first = await wiki_of_str(paper_id)

    bob = await register_and_login(client, email="wiki-bob@example.com")
    bob_headers = {"Authorization": f"Bearer {bob}"}
    resp = await client.post(
        f"/api/projects/{project_id}/members",
        json={"email": "wiki-bob@example.com", "role": "member"},
        headers=headers,
    )
    assert resp.status_code == 204, resp.text

    resp = await client.post(f"/api/papers/{paper_id}/recompile", headers=bob_headers)
    assert resp.status_code == 200, resp.text
    assert await _wiki_row_count(paper_id) == 1
    second = await wiki_of_str(paper_id)
    assert second.compiled_by != first.compiled_by  # 编译者换成最后那个人


# ---- 三条路径读到同一份 ----


async def test_same_wiki_from_library_daily_and_personal_library(client):
    """库详情 / 每日推送详情 / 个人库条目详情读到的是同一份解读。"""
    project_id, library_id, headers = await _setup(
        client, name="three-ways", email="wiki-three@example.com"
    )
    async with get_sessionmaker()() as session:
        paper = await add_paper(
            session,
            project_id=uuid.UUID(project_id),
            title="Three Ways Paper",
            arxiv_id="2601.00099",
            abstract="abstract",
            status="compiled",
            wiki_content="# 统一解读\n\n讲 [[Agent]]。",
        )
        await session.commit()
        paper_id = str(paper.id)
        # 同一篇进每日推送池
        from app.models.daily_feed import DailyFeedEntry

        session.add(
            DailyFeedEntry(
                paper_id=paper.id,
                feed_date=__import__("datetime").date(2026, 7, 25),
                primary_category="cs.AI",
                categories=["cs.AI"],
            )
        )
        await session.commit()
        entry_id = str(
            (
                await session.execute(
                    select(DailyFeedEntry.id).where(DailyFeedEntry.paper_id == paper.id)
                )
            ).scalar_one()
        )

    # ① 库内论文详情
    resp = await client.get(f"/api/papers/{paper_id}", headers=headers)
    assert resp.status_code == 200, resp.text
    wiki = resp.json()["wiki_content"]
    assert wiki == "# 统一解读\n\n讲 [[Agent]]。"

    # ② 每日推送详情（以前每日推送是另一份）
    resp = await client.get(f"/api/daily/papers/{entry_id}", headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["has_wiki"] is True
    assert resp.json()["wiki_content"] == wiki

    # ③ 个人库条目详情
    resp = await client.post(
        "/api/me/library/visits", json={"paper_id": paper_id}, headers=headers
    )
    assert resp.status_code == 201, resp.text
    entry = resp.json()
    resp = await client.get(f"/api/me/library/{entry['id']}", headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["wiki_content"] == wiki

    # ④ 课题相关研究书架
    resp = await client.post(
        f"/api/projects/{project_id}/shelf", json={"paper_id": paper_id}, headers=headers
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["wiki_content"] == wiki


async def test_daily_compile_is_visible_in_library_and_recompilable(client):
    """每日推送里编译 → 库里立刻看得到；已有解读也能再编译（覆盖同一行）。"""
    project_id, library_id, headers = await _setup(
        client, name="daily-shared", email="wiki-daily@example.com"
    )
    async with get_sessionmaker()() as session:
        paper = await add_paper(
            session,
            project_id=uuid.UUID(project_id),
            title="Daily Compiled Paper",
            abstract="abstract",
            status="scored",
        )
        await session.commit()
        paper_id = str(paper.id)
        from app.models.daily_feed import DailyFeedEntry

        entry = DailyFeedEntry(
            paper_id=paper.id,
            feed_date=__import__("datetime").date(2026, 7, 25),
            primary_category="cs.AI",
            categories=["cs.AI"],
        )
        session.add(entry)
        await session.commit()
        entry_id = str(entry.id)

    resp = await client.post(f"/api/daily/papers/{entry_id}/compile", headers=headers)
    assert resp.status_code == 200, resp.text
    compiled = resp.json()["wiki_content"]
    assert compiled.strip()
    assert await _wiki_row_count(paper_id) == 1

    resp = await client.get(f"/api/papers/{paper_id}", headers=headers)
    assert resp.json()["wiki_content"] == compiled  # 库里读到的是同一份

    # 已有解读不妨碍再编译：仍然一行，以最新为准
    resp = await client.post(f"/api/daily/papers/{entry_id}/compile", headers=headers)
    assert resp.status_code == 200, resp.text
    assert await _wiki_row_count(paper_id) == 1


# ---- 概念：论文级唯一，库作用域靠推导 ----


async def test_concept_shared_across_libraries_and_scoped_by_papers(client):
    """同名概念全平台一份；「哪个库有它」按库内论文的关联推导。"""
    token = await register_and_login(client, email="concept-unified@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    project_a, library_a = await make_project_with_library(client, headers, name="concept-a")
    project_b, library_b = await make_project_with_library(client, headers, name="concept-b")
    async with get_sessionmaker()() as session:
        await add_paper(
            session,
            project_id=uuid.UUID(project_a),
            title="Paper In A",
            status="compiled",
            wiki_content="讲 [[大语言模型]] 与 [[检索增强]]。",
        )
        await add_paper(
            session,
            project_id=uuid.UUID(project_b),
            title="Paper In B",
            status="compiled",
            wiki_content="也讲 [[大语言模型]]。",
        )
        await session.commit()

    for project_id in (project_a, project_b):
        resp = await client.post(f"/api/projects/{project_id}/concepts/relink", headers=headers)
        assert resp.status_code == 200, resp.text

    # 「大语言模型」两个库都用到，但全平台只有一行
    async with get_sessionmaker()() as session:
        rows = (
            (await session.execute(select(Concept).where(Concept.name == "大语言模型")))
            .scalars()
            .all()
        )
        assert len(rows) == 1
        concept_id = str(rows[0].id)

    names_a = {c["name"] for c in (await client.get(
        f"/api/libraries/{library_a}/concepts", headers=headers
    )).json()}
    names_b = {c["name"] for c in (await client.get(
        f"/api/libraries/{library_b}/concepts", headers=headers
    )).json()}
    assert names_a == {"大语言模型", "检索增强"}
    assert names_b == {"大语言模型"}  # B 库只用到一个

    # 概念详情：带库作用域只列该库的论文，不带就是全平台
    resp = await client.get(f"/api/concepts/{concept_id}", headers=headers)
    assert resp.status_code == 200, resp.text
    assert {p["title"] for p in resp.json()["papers"]} == {"Paper In A", "Paper In B"}
    resp = await client.get(
        f"/api/concepts/{concept_id}?library_id={library_b}", headers=headers
    )
    assert resp.status_code == 200, resp.text
    assert [p["title"] for p in resp.json()["papers"]] == ["Paper In B"]
    assert str(resp.json()["library_id"]) == str(library_b)


async def test_pool_only_paper_concepts_open_without_library(client):
    """不属于任何库的论文（每日推送口径）也能产生概念，概念页照常打开。"""
    token = await register_and_login(client, email="concept-pool@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    async with get_sessionmaker()() as session:
        from app.models.paper import new_paper

        paper = new_paper(title="Pool Only", abstract="a", source="manual")
        session.add(paper)
        await session.flush()
        concept = Concept(name="池内概念", slug="pool-concept", definition="d")
        session.add(concept)
        await session.flush()
        await session.execute(
            paper_concepts.insert().values(paper_id=paper.id, concept_id=concept.id)
        )
        await session.commit()
        concept_id = str(concept.id)

    resp = await client.get(f"/api/concepts/{concept_id}", headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["library_id"] is None  # 不属于任何库，但照常打开
    assert [p["title"] for p in body["papers"]] == ["Pool Only"]


async def test_lookup_concept_by_name_platform_wide(client):
    """池级 [[双链]] 的解析口径：按名字查全平台概念，查不到就是还没入库。"""
    token = await register_and_login(client, email="concept-lookup@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    async with get_sessionmaker()() as session:
        session.add(Concept(name="思维链", slug="思维链", definition="d", category="method"))
        await session.commit()

    resp = await client.get("/api/concepts?name=思维链", headers=headers)
    assert resp.status_code == 200, resp.text
    assert [c["name"] for c in resp.json()] == ["思维链"]
    assert resp.json()[0]["library_id"] is None  # 池级视角没有库作用域

    resp = await client.get("/api/concepts?name=还没入库的词", headers=headers)
    assert resp.status_code == 200 and resp.json() == []


async def test_lookup_concept_is_exact_not_substring(client):
    """按名字查是精确匹配：[[attention]] 不能命中「flash attention」。

    写法差异（空格/连接符）仍靠 slug 兜住，这是归一化不是子串。"""
    token = await register_and_login(client, email="concept-exact@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    async with get_sessionmaker()() as session:
        session.add_all(
            [
                Concept(name="Attention", slug="attention", definition="d", category="method"),
                Concept(
                    name="Flash Attention",
                    slug="flash-attention",
                    definition="d",
                    category="method",
                ),
                Concept(
                    name="Self-Attention",
                    slug="self-attention",
                    definition="d",
                    category="method",
                ),
            ]
        )
        await session.commit()

    # 精确（忽略大小写）：只回本尊，不带出「flash attention」
    resp = await client.get("/api/concepts?name=attention", headers=headers)
    assert [c["name"] for c in resp.json()] == ["Attention"]

    # 子串不算命中
    resp = await client.get("/api/concepts?name=flash", headers=headers)
    assert resp.json() == []
    resp = await client.get("/api/concepts?name=Attention 机制", headers=headers)
    assert resp.json() == []

    # 空格 / 连接符写法差异按 slug 归一化后仍能对上
    resp = await client.get("/api/concepts?name=self attention", headers=headers)
    assert [c["name"] for c in resp.json()] == ["Self-Attention"]
