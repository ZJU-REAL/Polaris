"""Obsidian vault 导出：内存构建 zip（docs/api-m2.md §5、docs/api-lit.md §6.6）。

vault 结构：
    index.md
    papers/<slug>.md    （frontmatter: title/arxiv_id/year/relevance/status/concepts）
    papers/figures/<paper_slug>-fig-<N>.png  （重要图 / 正文引用图）
    concepts/<slug>.md  （frontmatter: name/category）
    trends.md           （占位）
正文保留 [[wikilink]] 双链；``![[fig:N]]`` 重写为相对路径标准 markdown 图片，
正文没引用但 important 的图追加到「## 重要图片」小节。
"""

import io
import json
import uuid
import zipfile
from collections import defaultdict
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.paper import Concept, Paper, PaperNote
from app.models.project import Project
from app.models.user import User
from app.services.concepts import wiki_slug
from app.services.literature.pdf_extract import figure_path
from app.services.notes import author_name_of
from app.services.wiki_compile import FIGURE_MARKER_RE


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


def _inline_paper_figures(
    zf: zipfile.ZipFile, paper: Paper, slug: str, body: str
) -> tuple[str, list[str]]:
    """图文 wiki 导出（docs/api-lit.md §6.6）：打包 figure PNG 并重写正文标记。

    - 重要图与正文引用的图写入 zip ``papers/figures/<slug>-fig-<N>.png``；
    - ``![[fig:N]]`` 重写为 ``![fig N](figures/<slug>-fig-<N>.png)``（文件缺失则剥除标记）；
    - 返回 (重写后的正文, 正文没引用但 important 的图的追加行)。
    """
    fig_by_index = {int(f["index"]): f for f in (paper.figures or [])}
    referenced = {int(m.group(1)) for m in FIGURE_MARKER_RE.finditer(body)}
    packaged: dict[int, str] = {}  # index → zip 内文件名（相对 papers/figures/）
    wanted = referenced | {i for i, f in fig_by_index.items() if f.get("important")}
    for index in sorted(wanted & set(fig_by_index)):
        path = figure_path(str(paper.id), index)
        if not path.exists():
            continue
        name = f"{slug}-fig-{index}.png"
        zf.writestr(f"papers/figures/{name}", path.read_bytes())
        packaged[index] = name

    def _rewrite(match: Any) -> str:
        index = int(match.group(1))
        if index in packaged:
            return f"![fig {index}](figures/{packaged[index]})"
        return ""  # 引用的图未能打包（文件缺失/越界）→ 剥除标记

    body = FIGURE_MARKER_RE.sub(_rewrite, body)

    extra_lines: list[str] = []
    for index in sorted(set(packaged) - referenced):
        extra_lines.append(f"![fig {index}](figures/{packaged[index]})")
        if caption := fig_by_index[index].get("caption"):
            extra_lines.append(f"*{caption}*")
        extra_lines.append("")
    return body, extra_lines


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

    # 论文笔记（有笔记的论文页追加「## 笔记」小节）
    note_rows = await session.execute(
        select(PaperNote, User.display_name, User.email)
        .join(User, User.id == PaperNote.author_id)
        .where(PaperNote.project_id == project.id)
        .order_by(PaperNote.created_at)
    )
    notes_by_paper: dict[uuid.UUID, list[tuple[PaperNote, str]]] = defaultdict(list)
    for note, display_name, email in note_rows.all():
        notes_by_paper[note.paper_id].append((note, author_name_of(display_name, email)))

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
            if paper.figures:
                body, extra_figure_lines = _inline_paper_figures(zf, paper, slug, body)
                if extra_figure_lines:
                    body = (
                        body.rstrip("\n")
                        + "\n\n## 重要图片\n\n"
                        + "\n".join(extra_figure_lines).rstrip("\n")
                        + "\n"
                    )
            if notes := notes_by_paper.get(paper.id):
                lines = ["", "## 笔记", ""]
                for note, author_name in notes:
                    lines += [f"> **{author_name}** ({note.created_at:%Y-%m-%d})", ""]
                    lines += [note.content, ""]
                body = body.rstrip("\n") + "\n" + "\n".join(lines).rstrip("\n")
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
