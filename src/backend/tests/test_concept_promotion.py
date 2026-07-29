"""概念入库把关：「被 2 篇论文引用才复核转正」的门槛 + 转正时的有效性判定。

第 1 篇引用 → 候选（不可见、无定义）；第 2 篇引用 → 送模型复核，是概念就转正并生成
定义，不是概念（fig:1、编号、半句话）就落 rejected 终态。
"""

import uuid
from datetime import UTC, datetime
from pathlib import Path

from alembic.config import Config
from sqlalchemy import create_engine, select, text

from alembic import command
from app.core.db import get_sessionmaker
from app.core.llm.router import LLMRouter
from app.models.paper import Concept, Paper, PaperWiki
from app.services.concepts import (
    extract_wikilinks,
    link_generated_paper_concepts,
    link_paper_concepts,
)

from .conftest import add_paper, membership_of, register_and_login

BACKEND_DIR = Path(__file__).resolve().parent.parent


# ---- 1. 抽取只做确定性解析（配不配当概念交给复核那步判） ----


def test_extract_wikilinks_skips_embed_markers_only():
    md = "\n".join(
        [
            "正文提到 [[强化学习]]。",
            "![[fig:1]]",  # 正规图片标记
            "! [[fig:2]]",  # 感叹号与括号之间有空格，仍算图片标记
            "[[fig:3]] 漏了感叹号 —— 照常抽出来，由复核那步判掉",
            "还提到 [[Agent]] 与 [[RAG|检索增强]]。",
        ]
    )
    assert extract_wikilinks(md) == ["强化学习", "fig:3", "Agent", "RAG"]


# ---- 2. 转正门槛 + 有效性判定 ----


async def _seed(client, wiki_by_title: dict[str, str]) -> tuple[str, dict, dict[str, uuid.UUID]]:
    token = await register_and_login(client, email="promotion@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    resp = await client.post("/api/projects", json={"name": "gate-proj"}, headers=headers)
    project_id = resp.json()["id"]
    ids: dict[str, uuid.UUID] = {}
    async with get_sessionmaker()() as session:
        for title, wiki in wiki_by_title.items():
            paper = await add_paper(
                session,
                project_id=uuid.UUID(project_id),
                title=title,
                status="compiled",
                wiki_content=wiki,
            )
            ids[title] = paper.id
        await session.commit()
    return project_id, headers, ids


async def _link(project_id: str, paper_id: uuid.UUID) -> tuple[int, int]:
    async with get_sessionmaker()() as session:
        paper = (await session.execute(select(Paper).where(Paper.id == paper_id))).scalar_one()
        membership = await membership_of(session, project_id=project_id, paper_id=paper_id)
        return await link_paper_concepts(session, paper, membership, llm=LLMRouter())


async def _concept(name: str) -> Concept:
    async with get_sessionmaker()() as session:
        return (await session.execute(select(Concept).where(Concept.name == name))).scalar_one()


async def test_first_reference_stays_candidate_and_invisible(client):
    """第 1 篇引用：建候选词条 + 建关联，但不可见、不生成定义（也就不花那次调用）。"""
    project_id, headers, ids = await _seed(client, {"Paper A": "本文提出 [[世界模型]]。"})

    created, linked = await _link(project_id, ids["Paper A"])
    assert (created, linked) == (1, 1)

    concept = await _concept("世界模型")
    assert concept.status == "candidate"
    assert concept.definition is None and concept.validated_at is None

    # 概念库列表 / 按名查 / 详情 / 图谱：一律当作还没入库
    resp = await client.get(f"/api/projects/{project_id}/concepts", headers=headers)
    assert resp.json() == []
    resp = await client.get("/api/concepts?name=世界模型", headers=headers)
    assert resp.json() == []
    resp = await client.get(f"/api/concepts/{concept.id}", headers=headers)
    assert resp.status_code == 404
    graph = (await client.get(f"/api/projects/{project_id}/graph", headers=headers)).json()
    assert [n for n in graph["nodes"] if n["type"] == "concept"] == []
    # 论文详情上的概念标签同样只给正式概念
    detail = (await client.get(f"/api/papers/{ids['Paper A']}", headers=headers)).json()
    assert detail["concepts"] == []


async def test_second_reference_promotes_and_defines(client):
    """第 2 篇引用：复核通过 → 转正、生成定义，并在列表/按名查/图谱里出现。"""
    project_id, headers, ids = await _seed(
        client,
        {
            "Paper A": "本文提出 [[世界模型]] 与 [[本文自造Bench]]。",
            "Paper B": "沿用 [[世界模型]] 的思路。",
        },
    )
    await _link(project_id, ids["Paper A"])
    created, linked = await _link(project_id, ids["Paper B"])
    assert (created, linked) == (0, 1)  # 词条已有，只是多了一条关联

    concept = await _concept("世界模型")
    assert concept.status == "active"
    assert concept.definition == "世界模型 的一句话定义（fake）"
    assert concept.category == "method" and concept.validated_at is not None

    resp = await client.get(f"/api/projects/{project_id}/concepts", headers=headers)
    assert [(c["name"], c["paper_count"]) for c in resp.json()] == [("世界模型", 2)]
    resp = await client.get("/api/concepts?name=世界模型", headers=headers)
    assert [c["name"] for c in resp.json()] == ["世界模型"]
    assert (await client.get(f"/api/concepts/{concept.id}", headers=headers)).status_code == 200
    graph = (await client.get(f"/api/projects/{project_id}/graph", headers=headers)).json()
    assert [n["label"] for n in graph["nodes"] if n["type"] == "concept"] == ["世界模型"]

    # 只有 Paper A 起的那个名字仍留在候选（这正是要挡住的长尾）
    assert (await _concept("本文自造Bench")).status == "candidate"


async def test_daily_digest_concepts_link_and_promote_shared_names(client):
    """尚未精读的今日论文也能通过简报标注概念；只展示达到复用门槛的正式概念。"""
    project_id, _headers, ids = await _seed(
        client,
        {
            "Paper A": "尚未包含概念双链。",
            "Paper B": "同样尚未包含概念双链。",
        },
    )
    async with get_sessionmaker()() as session:
        active_by_paper, stats = await link_generated_paper_concepts(
            session,
            concepts_by_paper={
                ids["Paper A"]: ["Agent 工作流", "A 专属方法"],
                ids["Paper B"]: ["Agent 工作流", "B 专属方法"],
            },
            llm=LLMRouter(),
            project_id=uuid.UUID(project_id),
        )

    assert active_by_paper[str(ids["Paper A"])] == ["Agent 工作流"]
    assert active_by_paper[str(ids["Paper B"])] == ["Agent 工作流"]
    assert stats == {
        "concepts_created": 3,
        "links_created": 4,
        "concepts_promoted": 1,
        "concepts_rejected": 0,
        "active_concepts": 1,
    }
    assert (await _concept("Agent 工作流")).status == "active"
    assert (await _concept("A 专属方法")).status == "candidate"


async def test_junk_name_is_rejected_at_promotion(client):
    """够门槛也进不来：模型判定不是概念的（漏感叹号的 fig:3）落 rejected 终态。

    这类名字被几十篇论文各标一次，门槛拦不住，只能靠复核那步。"""
    project_id, headers, ids = await _seed(
        client,
        {
            "Paper A": "见 [[fig:3]] 与 [[世界模型]]。",
            "Paper B": "另见 [[fig:3]] 和 [[世界模型]]。",
        },
    )
    await _link(project_id, ids["Paper A"])
    await _link(project_id, ids["Paper B"])

    junk = await _concept("fig:3")
    assert junk.status == "rejected"
    assert junk.validated_at is not None  # 终态：判过就不再复判
    resp = await client.get(f"/api/projects/{project_id}/concepts", headers=headers)
    assert [c["name"] for c in resp.json()] == ["世界模型"]
    assert (await client.get(f"/api/concepts/{junk.id}", headers=headers)).status_code == 404
    # 关联不删（不删任何数据），只是这个词条永远不可见
    async with get_sessionmaker()() as session:
        from app.models.paper import paper_concepts

        rows = (
            await session.execute(
                select(paper_concepts.c.paper_id).where(paper_concepts.c.concept_id == junk.id)
            )
        ).all()
        assert len(rows) == 2


async def test_promotion_survives_recompile_of_one_paper(client):
    """转正后不会因为某一篇改写正文而回退（不做降级，避免概念库反复闪现）。"""
    project_id, headers, ids = await _seed(
        client,
        {"Paper A": "讲 [[世界模型]]。", "Paper B": "也讲 [[世界模型]]。"},
    )
    await _link(project_id, ids["Paper A"])
    await _link(project_id, ids["Paper B"])

    async with get_sessionmaker()() as session:
        wiki = (
            await session.execute(select(PaperWiki).where(PaperWiki.paper_id == ids["Paper A"]))
        ).scalar_one()
        wiki.content = "改写后不再提它。"
        await session.commit()
    await _link(project_id, ids["Paper A"])

    concept = await _concept("世界模型")
    assert concept.status == "active"
    resp = await client.get(f"/api/projects/{project_id}/concepts", headers=headers)
    assert [(c["name"], c["paper_count"]) for c in resp.json()] == [("世界模型", 1)]


async def test_sweep_reviews_never_validated_legacy_concepts(client):
    """存量清理：迁移留下的 active + validated_at 为空的老词条，同步时复核一遍。

    生产库里 fig:1 被 35 篇论文引用，迁移按关联数会把它判成 active——正是靠这一步下架。"""
    project_id, headers, _ids = await _seed(
        client,
        {
            "Paper A": "见 [[fig:1]]、[[真概念]] 与 [[世界模型]]。",
            "Paper B": "另见 [[fig:1]] 和 [[真概念]]。",
        },
    )
    async with get_sessionmaker()() as session:
        # 造迁移后的存量形态：已转正、有定义、但从没判过有效性（validated_at 为空）
        session.add_all(
            [
                Concept(name="fig:1", slug="fig-1", definition="旧定义", status="active"),
                Concept(name="真概念", slug="real-one", definition="旧定义", status="active"),
            ]
        )
        await session.commit()

    resp = await client.post(f"/api/projects/{project_id}/concepts/relink", headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["rejected_concepts"] == ["fig:1"] and body["concepts_rejected"] == 1

    assert (await _concept("fig:1")).status == "rejected"
    real = await _concept("真概念")
    assert real.status == "active" and real.validated_at is not None  # 判过就不再复判
    names = {
        c["name"]
        for c in (await client.get(f"/api/projects/{project_id}/concepts", headers=headers)).json()
    }
    assert "fig:1" not in names and "真概念" in names


# ---- 3. 迁移：按现有关联数重判存量，一行不删 ----

HEAD_REVISION = "a9f1c62b70d5"  # 概念转正门槛
BEFORE_REVISION = "c4e7b2a91f38"  # 上一版


def _make_config(db_path: Path) -> Config:
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{db_path}")
    return cfg


def _rows(db_path: Path, sql: str) -> list[tuple]:
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        with engine.connect() as conn:
            return list(conn.execute(text(sql)).all())
    finally:
        engine.dispose()


def _seed_concepts(db_path: Path) -> dict[str, uuid.UUID]:
    """存量：3 篇论文 + 引用数 1/2/3 的概念 + 一个被 2 篇引用的 fig:1 脏词条。"""
    ids = {key: uuid.uuid4() for key in ("p1", "p2", "p3", "solo", "pair", "trio", "junk")}
    engine = create_engine(f"sqlite:///{db_path}")
    now = datetime(2026, 7, 1, tzinfo=UTC).isoformat()
    links = {
        "solo": ["p1"],
        "pair": ["p1", "p2"],
        "trio": ["p1", "p2", "p3"],
        "junk": ["p1", "p2"],
    }
    try:
        with engine.begin() as conn:
            for key in ("p1", "p2", "p3"):
                conn.execute(
                    text(
                        "INSERT INTO papers (id, title, created_at, updated_at)"
                        " VALUES (:id, :title, :t, :t)"
                    ),
                    {"id": ids[key].hex, "title": key, "t": now},
                )
            for key, name in (
                ("solo", "只有一篇用的词"),
                ("pair", "两篇都用的词"),
                ("trio", "三篇都用的词"),
                ("junk", "fig:1"),
            ):
                conn.execute(
                    text(
                        "INSERT INTO concepts (id, name, slug, definition, category,"
                        " created_at, updated_at)"
                        " VALUES (:id, :name, :slug, :d, 'method', :t, :t)"
                    ),
                    {
                        "id": ids[key].hex,
                        "name": name,
                        "slug": key,
                        "d": f"{name} 的定义",
                        "t": now,
                    },
                )
            for concept, papers in links.items():
                for paper in papers:
                    conn.execute(
                        text("INSERT INTO paper_concepts (paper_id, concept_id) VALUES (:p, :c)"),
                        {"p": ids[paper].hex, "c": ids[concept].hex},
                    )
    finally:
        engine.dispose()
    return ids


def test_migration_rejudges_by_link_count_without_losing_rows(tmp_path):
    db_path = tmp_path / "gate.db"
    cfg = _make_config(db_path)
    command.upgrade(cfg, BEFORE_REVISION)
    ids = _seed_concepts(db_path)

    command.upgrade(cfg, HEAD_REVISION)
    rows = {
        row[0]: (row[1], row[2], row[3])
        for row in _rows(db_path, "SELECT name, status, definition, validated_at FROM concepts")
    }
    assert rows["只有一篇用的词"][0] == "candidate"
    assert rows["两篇都用的词"][0] == "active"
    assert rows["三篇都用的词"][0] == "active"
    # fig:1 靠关联数拦不住（生产库里它有 35 篇引用）——迁移只按数量重判，
    # validated_at 留空作为「还没判过有效性」的标记，交给下一次同步复核下架
    assert rows["fig:1"][0] == "active"
    assert all(row[2] is None for row in rows.values())
    # 一行不删，降级为候选的定义（已经花过钱了）原样保留
    assert len(rows) == 4
    assert rows["只有一篇用的词"][1] == "只有一篇用的词 的定义"
    assert len(_rows(db_path, "SELECT * FROM paper_concepts")) == 8

    # downgrade：全部置回 active（= 迁移前「都可见」）后删列，数据仍是 4 行 8 关联
    command.downgrade(cfg, BEFORE_REVISION)
    columns = {row[1] for row in _rows(db_path, "PRAGMA table_info(concepts)")}
    assert not {"status", "validated_at"} & columns
    assert len(_rows(db_path, "SELECT id FROM concepts")) == 4
    assert len(_rows(db_path, "SELECT * FROM paper_concepts")) == 8
    assert {row[0] for row in _rows(db_path, "SELECT id FROM concepts")} == {
        ids[key].hex for key in ("solo", "pair", "trio", "junk")
    }
