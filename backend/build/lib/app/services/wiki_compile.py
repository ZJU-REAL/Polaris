"""图文交织 wiki 编译（docs/api-lit.md §6.6）：Librarian 看图写作。

流程（wiki.compile 步骤与 POST /papers/{id}/recompile 共用）：
    ① figures 未注释先筛选注释（stage=librarian 多模态）；
    ② 编译调用带上重要图（≤4 张）与图注清单，要求正文插入 ``![[fig:N]]`` 标记；
    ③ 写 wiki_content 前校验标记：index 不在 figures 里的整行剥除。
无 PDF / 无图时退化为纯文字编译（正文优先全文，缺全文用摘要）。
"""

import re
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.llm.base import Message
from app.core.llm.router import LLMRouter, get_llm_router
from app.models.base import utcnow
from app.models.paper import Paper
from app.models.project import Project
from app.services.figure_annotate import (
    annotate_figures,
    important_figures_with_bytes,
)
from app.services.literature.pdf_extract import extract_figures

FULLTEXT_PROMPT_CHARS = 16000

# 行内图片标记 ![[fig:N]]（N = Paper.figures 的 index）
FIGURE_MARKER_RE = re.compile(r"!\[\[fig:(\d+)\]\]")

LIBRARIAN_SYSTEM_PROMPT = """\
你是 Librarian，负责把一篇论文编译成中文 wiki 页（markdown，正文优先引用全文）。
按以下结构输出（保留标题层级）：
## TL;DR
## 研究动机
## 方法
## 实验结论
## 可借鉴点
## 相关概念
文中出现的关键概念（方法/架构/问题/指标/数据集等）用双链 [[概念名]] 标注，
「相关概念」一节列出全部双链。
"""


def strip_invalid_figure_markers(content: str, valid_indices: set[int]) -> str:
    """剥除引用不存在 index 的 ``![[fig:N]]`` 标记：含无效标记的行整行删掉。"""
    lines: list[str] = []
    for line in content.splitlines():
        markers = [int(m.group(1)) for m in FIGURE_MARKER_RE.finditer(line)]
        if markers and any(i not in valid_indices for i in markers):
            continue
        lines.append(line)
    return "\n".join(lines) + ("\n" if content.endswith("\n") else "")


def _figure_prompt_section(selected: list[tuple[dict[str, Any], bytes]]) -> str:
    lines = ["可用图片清单（与附带图片顺序一致）："]
    for fig, _ in selected:
        caption = fig.get("caption") or f"第 {fig.get('page')} 页插图"
        lines.append(f"- fig:{int(fig['index'])} —— {caption}")
    lines.append(
        "请在正文最相关的段落后插入独立一行 ![[fig:N]]"
        "（只用清单中的 N，2-4 处，不要集中堆在开头或结尾）。"
    )
    return "\n".join(lines)


def build_compile_prompt(paper: Paper, *, statement: str) -> tuple[str, list[bytes]]:
    """组装编译 user prompt 与随附图片（无重要图时 images 为空 → 纯文字编译）。"""
    body: str | None = None
    source = "abstract"
    if paper.full_text_path and Path(paper.full_text_path).exists():
        body = Path(paper.full_text_path).read_text(encoding="utf-8", errors="ignore")
        source = "full_text"
    body = (body or paper.abstract or "（无正文）")[:FULLTEXT_PROMPT_CHARS]
    authors = "、".join(a.get("name", "") for a in (paper.authors or []) if isinstance(a, dict))
    prompt = (
        f"研究方向：{statement}\n"
        f"标题：{paper.title}\n"
        f"作者：{authors or '未知'}\n"
        f"年份/发表：{paper.year or '未知'} {paper.venue or ''}\n"
        f"正文来源：{source}\n"
        f"正文：\n{body}"
    )
    selected = important_figures_with_bytes(paper)
    if selected:
        prompt += "\n\n" + _figure_prompt_section(selected)
    return prompt, [data for _, data in selected]


async def compile_paper(
    paper: Paper,
    *,
    statement: str,
    llm: LLMRouter | None = None,
    user_id: uuid.UUID | None = None,
    voyage_id: uuid.UUID | None = None,
) -> str:
    """图文编译一篇论文，返回校验过标记的 wiki markdown（调用方负责落库）。"""
    llm = llm or get_llm_router()
    user_prompt, images = build_compile_prompt(paper, statement=statement)
    result = await llm.complete(
        "librarian",
        [
            Message(role="system", content=LIBRARIAN_SYSTEM_PROMPT),
            Message(role="user", content=user_prompt),
        ],
        images=images or None,
        user_id=user_id,
        project_id=paper.project_id,
        voyage_id=voyage_id,
    )
    if not result.content.strip():
        raise ValueError("librarian returned empty content")
    valid = {int(f["index"]) for f in (paper.figures or [])}
    return strip_invalid_figure_markers(result.content, valid)


async def recompile_paper(
    session: AsyncSession,
    paper: Paper,
    *,
    user_id: uuid.UUID | None = None,
) -> Paper:
    """重跑筛选注释 + 图文编译，覆盖 wiki_content 并落库（docs/api-lit.md §6.6）。

    无 PDF 时跳过图片、仅重写文字；status：scored/fetched 升为 compiled，其余不动。
    """
    llm = get_llm_router()
    project = await session.get(Project, paper.project_id)
    definition = project.definition if project and isinstance(project.definition, dict) else {}
    statement = definition.get("statement") or (project.name if project else paper.title)

    if paper.pdf_path and Path(paper.pdf_path).exists():
        if paper.figures is None:
            candidates = await extract_figures(str(paper.id), Path(paper.pdf_path))
            paper.figures = [c | {"caption": None, "important": False} for c in candidates]
        if paper.figures:
            await annotate_figures(paper, paper.figures, llm=llm, user_id=user_id)
        await session.commit()

    content = await compile_paper(paper, statement=statement, llm=llm, user_id=user_id)
    paper.wiki_content = content
    paper.compiled_at = utcnow()
    if paper.status in ("scored", "fetched"):
        paper.status = "compiled"
    await session.commit()
    return paper
