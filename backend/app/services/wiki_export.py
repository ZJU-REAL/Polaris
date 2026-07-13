"""Obsidian vault 导出：内存构建 zip（docs/api-m2.md §5）。

vault 结构：
    index.md
    papers/<slug>.md    （frontmatter: title/arxiv_id/year/relevance/status/concepts）
    concepts/<slug>.md  （frontmatter: name/category）
    trends.md           （占位）
正文保留 [[wikilink]] 双链。
"""

import io
import json
import uuid
import zipfile
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.paper import Concept, Paper
from app.models.project import Project
from app.services.concepts import wiki_slug


def _yaml_value(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return str(value)
    return json.dumps(str(value), ensure_ascii=False)  # JSON 字符串是合法 YAML 标量


def _frontmatter(fields: dict[str, Any]) -> str:
    lines = ["---"]
    for key, value in fields.items():
        if isinstance(value, list):
            lines.append(f"{key}:")
            lines.extend(f"  - {_yaml_value(v)}" for v in value)
        else:
            lines.append(f"{key}: {_yaml_value(value)}")
    lines.append("---")
    return "\n".join(lines) + "\n\n"


def _unique_slug(base: str, used: set[str]) -> str:
    slug = base[:80].strip("-") or "untitled"
    if slug in used:
        slug = f"{slug}-{uuid.uuid4().hex[:6]}"
    used.add(slug)
    return slug


async def build_obsidian_zip(session: AsyncSession, project: Project) -> bytes:
    papers = (
        (
            await session.execute(
                select(Paper)
                .where(
                    Paper.project_id == project.id,
                    Paper.status.in_(("compiled", "included")),
                )
                .options(selectinload(Paper.concepts))
                .order_by(Paper.relevance_score.desc().nulls_last())
            )
        )
        .scalars()
        .all()
    )
    concepts = (
        (
            await session.execute(
                select(Concept)
                .where(Concept.project_id == project.id)
                .options(selectinload(Concept.papers))
                .order_by(Concept.name)
            )
        )
        .scalars()
        .all()
    )

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        used_paper_slugs: set[str] = set()
        paper_entries: list[tuple[str, Paper]] = [
            (_unique_slug(wiki_slug(p.title), used_paper_slugs), p) for p in papers
        ]

        # index.md
        index_lines = [f"# {project.name} · Research Wiki", ""]
        index_lines += ["## Papers", ""]
        index_lines += [f"- [[{slug}]] — {p.title}" for slug, p in paper_entries] or ["（暂无）"]
        index_lines += ["", "## Concepts", ""]
        index_lines += [f"- [[{c.slug}]] — {c.name}" for c in concepts] or ["（暂无）"]
        index_lines += ["", "另见 [[trends]]。", ""]
        zf.writestr("index.md", "\n".join(index_lines))

        # papers/<slug>.md
        for slug, paper in paper_entries:
            fm = _frontmatter(
                {
                    "title": paper.title,
                    "arxiv_id": paper.arxiv_id,
                    "year": paper.year,
                    "relevance": paper.relevance_score,
                    "status": paper.status,
                    "concepts": [c.name for c in paper.concepts],
                }
            )
            body = paper.wiki_content or (paper.abstract or "（尚未编译 wiki 页）")
            zf.writestr(f"papers/{slug}.md", fm + body + "\n")

        # concepts/<slug>.md
        for concept in concepts:
            fm = _frontmatter({"name": concept.name, "category": concept.category})
            lines = [f"# {concept.name}", ""]
            if concept.definition:
                lines += [f"> {concept.definition}", ""]
            if concept.wiki_content:
                lines += [concept.wiki_content, ""]
            paper_slug_by_id = {p.id: slug for slug, p in paper_entries}
            refs = [
                f"- [[{paper_slug_by_id[p.id]}]] — {p.title}"
                for p in concept.papers
                if p.id in paper_slug_by_id
            ]
            if refs:
                lines += ["## 出现于论文", "", *refs, ""]
            zf.writestr(f"concepts/{concept.slug}.md", fm + "\n".join(lines))

        # trends.md（M2 占位）
        zf.writestr(
            "trends.md",
            "# 趋势 Trends\n\n（占位：趋势分析将在后续里程碑生成。）\n",
        )

    return buf.getvalue()
