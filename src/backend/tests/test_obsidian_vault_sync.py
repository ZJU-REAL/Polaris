import json
import uuid
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.core.db import get_sessionmaker
from app.models.library_direction import DirectionLibrary, LibraryPaper, TopicSourceLibrary
from app.models.paper import Concept, Paper, PaperChunk, PaperWiki, paper_concepts
from app.models.project import Project
from app.models.research_digest import LibraryResearchDigest
from app.models.user import User
from app.services.obsidian_vault_sync import render_markdown, scan_vault, sync_obsidian_vault


def _make_vault(root: Path) -> Path:
    for relative in (
        "raw/meta",
        "raw/paper",
        "raw/text",
        "raw/figures",
        "wiki/papers",
        "wiki/surveys",
        "wiki/concepts",
        "report",
    ):
        (root / relative).mkdir(parents=True)
    (root / "research_direction.md").write_text(
        "# Direction\n\n## 一句话\n研究 agent harness。\n\n## 范围\n测试。\n",
        encoding="utf-8",
    )
    meta = {
        "arxiv_id": "2501.01234v2",
        "title": "Harness Paper",
        "abstract": "An abstract",
        "authors": ["Ada Lovelace"],
        "categories": ["cs.AI"],
        "published": "2025-01-02",
        "relevance_score": 0.9,
        "slug": "harness-paper",
    }
    (root / "raw/meta/harness-paper.json").write_text(json.dumps(meta), encoding="utf-8")
    (root / "raw/paper/harness-paper.pdf").write_bytes(b"%PDF-test")
    (root / "raw/text/harness-paper.txt").write_text(
        "First paragraph.\n\nSecond paragraph.", encoding="utf-8"
    )
    (root / "raw/figures/harness-paper.png").write_bytes(b"fake-png")
    (root / "raw/figures/harness-paper.json").write_text(
        json.dumps({"page": 2, "w": 640, "h": 480, "caption": "Architecture"}),
        encoding="utf-8",
    )
    (root / "wiki/concepts/agent-harness.md").write_text(
        "---\ntype: concept\n---\n# Agent Harness\n\n**定义**：Agent 运行时。\n",
        encoding="utf-8",
    )
    (root / "wiki/papers/harness-paper.md").write_text(
        """---
type: paper
arxiv_id: "2501.01234"
---
# Harness Paper

## TL;DR
一篇关于 harness 的论文。

## 架构图
![架构图](../../raw/figures/harness-paper.png)

## 相关概念
- [[concepts/agent-harness]]

---
本地原文：[PDF](../../raw/paper/harness-paper.pdf)
""",
        encoding="utf-8",
    )
    (root / "report/2025-01-03.md").write_text(
        """# 每日简报

候选 3 → +1

## 本期观察

Agent harness 开始强调可验证的运行时。

- [[papers/harness-paper|Harness Paper]] 给出运行时方案。

## 排除清单（3 候选 − 1 入库 = 2 篇）

- 两篇弱相关论文。
""",
        encoding="utf-8",
    )
    (root / "wiki/trends.md").write_text(
        "# 趋势综述\n\n## 可验证运行时\n\n与 [[concepts/agent-harness]] 相关。\n",
        encoding="utf-8",
    )
    return root


def test_scan_and_render_vault(tmp_path):
    snapshot = scan_vault(_make_vault(tmp_path / "vault"))
    assert len(snapshot.papers) == 1
    assert snapshot.papers[0].arxiv_id == "2501.01234"
    assert snapshot.papers[0].concept_slugs == {"agent-harness"}
    assert [report.report_date.isoformat() for report in snapshot.reports] == ["2025-01-03"]
    assert snapshot.trends_raw
    rendered = render_markdown(snapshot.papers[0].wiki_raw or "", snapshot=snapshot, figure_index=3)
    assert not rendered.startswith("---")
    assert "![[fig:3]]" in rendered
    assert "[[Agent Harness]]" in rendered
    assert "本地原文" not in rendered


@pytest.mark.asyncio
async def test_sync_is_idempotent(app, tmp_path):
    vault = _make_vault(tmp_path / "vault")
    owner_id = uuid.uuid4()
    project_id = uuid.uuid4()
    async with get_sessionmaker()() as session:
        owner = User(
            id=owner_id,
            email="owner@example.com",
            hashed_password="test",
            is_active=True,
            is_superuser=False,
            is_verified=True,
            display_name="Owner",
        )
        session.add(owner)
        await session.flush()
        session.add(
            Project(
                id=project_id,
                name="Agent Harness",
                slug=f"agent-harness-{project_id.hex[:8]}",
                owner_id=owner_id,
            )
        )
        await session.commit()

        first = await sync_obsidian_vault(
            session,
            vault_path=vault,
            project_id=project_id,
            batch_size=1,
        )
        assert first.library_created is True
        assert first.new_papers == 1
        assert first.memberships_created == 1
        assert first.wikis_created == 1
        assert first.pdfs_copied == 1
        assert first.texts_copied == 1
        assert first.figures_copied == 1
        assert first.concepts_created == 1
        assert first.concept_links_created == 1
        assert first.chunks_created == 1
        assert first.reports_created == 1
        second = await sync_obsidian_vault(
            session,
            vault_path=vault,
            project_id=project_id,
            batch_size=1,
        )
        assert second.library_created is False
        assert second.new_papers == 0
        assert second.reused_papers == 1
        assert second.memberships_created == 0
        assert second.wikis_created == 0
        assert second.wikis_preserved == 1
        assert second.pdfs_copied == 0
        assert second.texts_copied == 0
        assert second.figures_copied == 0
        assert second.concept_links_created == 0
        assert second.chunks_created == 0
        assert second.reports_created == 0
        assert second.reports_updated == 0

        digest = (await session.execute(select(LibraryResearchDigest))).scalar_one()
        paper = (await session.execute(select(Paper))).scalar_one()
        assert f"[[paper:{paper.id}|Harness Paper]]" in digest.content
        assert digest.paper_insights == [
            {
                "paper_id": str(paper.id),
                "title": "Harness Paper",
                "tldr": "一篇关于 harness 的论文。",
                "relevance_score": 0.9,
                "status": "compiled",
            }
        ]

        singleton_models = (
            DirectionLibrary,
            LibraryPaper,
            TopicSourceLibrary,
            Paper,
            PaperWiki,
            Concept,
            LibraryResearchDigest,
        )
        for model in singleton_models:
            assert (await session.scalar(select(func.count()).select_from(model))) == 1
        assert (await session.scalar(select(func.count()).select_from(PaperChunk))) == 1
        assert (await session.scalar(select(func.count()).select_from(paper_concepts))) == 1
