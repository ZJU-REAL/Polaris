"""图文交织 wiki 编译（docs/api-lit.md §6.6）：Librarian 看图写作。

流程（wiki.compile 步骤与 POST /papers/{id}/recompile 共用）：
    ① figures 未注释先筛选注释（stage=librarian 多模态）；
    ② 编译调用带上重要图（≤4 张）与图注清单，要求正文插入 ``![[fig:N]]`` 标记；
    ③ 写 wiki_content 前校验标记：index 不在 figures 里的整行剥除。
无 PDF / 无图时退化为纯文字编译（正文优先全文，缺全文用摘要）。
"""

import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.llm.base import Message
from app.core.llm.router import LLMRouter, get_llm_router
from app.models.base import utcnow
from app.models.paper import Paper
from app.models.project import Project
from app.services.concepts import link_paper_concepts
from app.services.figure_annotate import (
    FIGURE_KIND_ZH,
    annotate_figures,
    important_figures_with_bytes,
)
from app.services.literature.pdf_extract import extract_figures

FULLTEXT_PROMPT_CHARS = 24000

# 行内图片标记 ![[fig:N]]（N = Paper.figures 的 index）
FIGURE_MARKER_RE = re.compile(r"!\[\[fig:(\d+)\]\]")

LIBRARIAN_SYSTEM_PROMPT = """\
你是 Librarian，负责把一篇论文写成一篇深入浅出的中文解读文章（markdown，正文优先引用全文）。
像优秀的技术博客那样行文：用连贯的多段落叙述展开，段落之间自然衔接、有承接的逻辑线，
不要写成要点提纲；除必要的公式或代码外尽量少用列表。
结构骨架（保留二级标题层级，小标题措辞可按论文内容微调）：
## TL;DR
两三句话说清这篇论文做了什么、结果如何。
## 研究背景与动机
这个问题为什么重要、已有方法卡在哪里、这篇论文的切入点是什么。
## 方法
核心思路是怎么来的，关键设计逐步展开讲透（为什么这样设计、和直觉做法差在哪）。
## 实验与结果
实验设置、主要数字与对比、这些结果说明了什么。
## 讨论与可借鉴点
局限、未解决的问题，以及对当前研究方向的启发。
写作要求：
- 篇幅充分展开（通常 800–1500 字）；有全文时要利用正文细节，不要只复述摘要；
- 文中出现的关键概念（方法/架构/问题/指标/数据集等）在首次出现处用双链 [[概念名]] 就地标注；
- 不要在文末单独罗列「相关概念」清单，双链只放在正文叙述里；
- 数学符号与公式用 LaTeX：行内 $...$，重要公式独立一行用 $$...$$；
- 若随消息附带了论文配图，文章必须图文交织：把每张图插到对应小节，并用文字介绍这张图
  （具体规则见「可用图片清单」后的说明）。
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


# 图片类型 → 建议落位的小节（编译 prompt 用）
_KIND_SECTION_HINT = {
    "motivation": "研究背景与动机",
    "method": "方法",
    "architecture": "方法",
    "experiment": "实验与结果",
    "other": "最相关的小节",
}


def _figure_prompt_section(selected: list[tuple[dict[str, Any], bytes]]) -> str:
    lines = ["可用图片清单（与附带图片顺序一致）："]
    for fig, _ in selected:
        kind = str(fig.get("kind") or "other")
        kind_zh = FIGURE_KIND_ZH.get(kind, "其他")
        section = _KIND_SECTION_HINT.get(kind, "最相关的小节")
        caption = fig.get("caption") or f"第 {fig.get('page')} 页插图"
        lines.append(f"- fig:{int(fig['index'])}【{kind_zh}，建议放在「{section}」】—— {caption}")
    lines.append(
        "图文交织要求：\n"
        "1. 清单里的每一张图都要用上：在建议的小节里插入独立一行 ![[fig:N]]（只用清单中的 N）；\n"
        "2. 每处插图必须配文字介绍——插图前后用 1-3 句话讲清这张图画了什么、"
        "如何支撑正文的论点、读者应该注意图中哪个部分，行文像「如图所示，……」的博客写法；\n"
        "3. 不要只丢一个图标记不解释，也不要把图集中堆在文章开头或结尾。"
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


@dataclass(slots=True)
class CompiledWiki:
    """compile_paper 结果：校验过标记的 wiki markdown + 实际使用的模型名。"""

    content: str
    model: str


async def compile_paper(
    paper: Paper,
    *,
    statement: str,
    llm: LLMRouter | None = None,
    user_id: uuid.UUID | None = None,
    voyage_id: uuid.UUID | None = None,
    extra_guidance: str = "",
) -> CompiledWiki:
    """图文编译一篇论文，返回校验过标记的 wiki markdown 与所用模型（调用方负责落库）。

    模型名取自 LLM 实际返回结果（CompletionResult.model），而非路由表配置，
    避免路由中途被改导致记录偏差。
    extra_guidance：追加到 system prompt 的补充指引（wiki.compile 注入点的项目技能）。
    """
    llm = llm or get_llm_router()
    user_prompt, images = build_compile_prompt(paper, statement=statement)
    valid = {int(f["index"]) for f in (paper.figures or [])}

    content = ""
    model = ""
    for attempt in range(2):
        prompt = user_prompt
        if attempt == 1:
            prompt += (
                "\n\n注意：上一稿没有在正文里插入任何图片标记。请重写全文，"
                "务必按「可用图片清单」把每张图以独立一行 ![[fig:N]] 插到对应小节，"
                "并在插图前后用文字介绍这张图。"
            )
        result = await llm.complete(
            "librarian",
            [
                Message(role="system", content=LIBRARIAN_SYSTEM_PROMPT + extra_guidance),
                Message(role="user", content=prompt),
            ],
            images=images or None,
            user_id=user_id,
            project_id=paper.project_id,
            voyage_id=voyage_id,
        )
        if not result.content.strip():
            raise ValueError("librarian returned empty content")
        content = strip_invalid_figure_markers(result.content, valid)
        model = result.model
        # 有配图但一张都没插 → 带强指令重写一次；仍失败则接受纯文字稿（图库里还能看图）
        if not images or FIGURE_MARKER_RE.search(content):
            break
    return CompiledWiki(content=content, model=model)


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
        # 从未提取过，或上一轮一张重要图都没选出来（多为旧提取逻辑漏掉矢量图）→ 重提候选
        if paper.figures is None or not any(f.get("important") for f in paper.figures):
            candidates = await extract_figures(str(paper.id), Path(paper.pdf_path))
            paper.figures = [
                c | {"caption": None, "kind": None, "important": False} for c in candidates
            ]
        if paper.figures:
            await annotate_figures(paper, paper.figures, llm=llm, user_id=user_id)
        await session.commit()

    compiled = await compile_paper(paper, statement=statement, llm=llm, user_id=user_id)
    paper.wiki_content = compiled.content
    paper.compiled_at = utcnow()
    paper.compiled_model = compiled.model or None
    if paper.status in ("scored", "fetched"):
        paper.status = "compiled"
    await session.commit()
    # 单篇概念上链：新介绍里的 [[双链]] 建词条并关联（否则点击提示"概念尚未入库"）
    await link_paper_concepts(session, paper, llm=llm, user_id=user_id)
    return paper
