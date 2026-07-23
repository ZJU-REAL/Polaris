# ruff: noqa: E501 — prompt 文案里的 JSON 结构示例保持单行更可读
"""论文分享 PPT 动作（kind=presentation）：collect → outline → slides → build。

- 判断性环节（大纲/甲板内容/视觉评审）走 LLM（stage=librarian，多模态），
  注入点 present.outline / present.slides 可挂技能（如「论文分享 PPT 规范」）；
- 确定性环节（取材、版式渲染、字号约束、文本校验、soffice 渲染）在
  services/presentation.py；
- 反馈迭代：①文本校验不过 → 带诊断让 LLM 修甲板（≤2 轮）；
  ②soffice 渲染成图 → VLM 检查布局/文字可读性 → 不过让 LLM 修（≤2 轮，无 soffice 降级跳过）。
"""

import json
import logging
import uuid
from pathlib import Path
from typing import Any

from app.agents.voyage.actions import ActionContext, register
from app.core.config import get_settings
from app.core.db import get_sessionmaker
from app.core.llm.base import Message
from app.models.paper import Paper
from app.services.figure_annotate import important_figures_with_bytes
from app.services.libraries import (
    dedupe_member_rows,
    get_source_library_ids,
    member_papers_stmt,
)
from app.services.presentation import (
    DeckSpec,
    build_deck,
    render_slide_images,
    soffice_available,
    validate_deck_spec,
)

logger = logging.getLogger(__name__)

_MAX_JSON_ATTEMPTS = 3
_SINGLE_BODY_CHARS = 12000
_SURVEY_BODY_CHARS = 4000
_FIX_ROUNDS = 2
_STAGE = "librarian"  # 多模态路由（大纲/甲板/视觉评审共用）

DECK_JSON_SPEC = """\
严格输出一个 JSON 对象（不要 markdown 围栏），slides 里每页选一种 kind：
{"title": "PPT 总标题",
 "slides": [
   {"kind": "cover", "title": "短标题", "subtitle": "会议/年份", "presenter": "汇报人：…", "date": "…", "notes": "开场词"},
   {"kind": "toc", "title": "目录", "items": ["3-5 条"]},
   {"kind": "section", "title": "1. 章节名", "subtitle": "01 章节名"},
   {"kind": "content", "title": "…", "bullets": ["动机｜引导词加正文的要点写法", "- 二级细节"], "summary": "总结｜可选的页底总结条", "notes": "这页要说的话"},
   {"kind": "cards", "title": "…", "cards": [{"title": "卡片小标题", "body": "≤90 字说透一件事"}], "summary": "…"},
   {"kind": "columns", "title": "…", "columns": [{"header": "栏目名", "bullets": ["≤44 字短句"]}]},
   {"kind": "compare", "title": "A vs B", "left": {"header": "方法A", "bullets": ["…"]}, "right": {"header": "方法B", "bullets": ["…"]}},
   {"kind": "table", "title": "…", "table": {"headers": ["…"], "rows": [["单元格 ≤30 字"]]}},
   {"kind": "figure", "title": "…", "figure_index": 0, "caption": "≥15 字：图画了什么、支撑什么论点、看图中哪里", "notes": "…"},
   {"kind": "closing", "title": "谢谢", "subtitle": "…"}]}
版式选择（布局和谐的关键，渲染器会校验）：
- 并列的 3-6 件事（方法组件/问题/趋势）用 cards；带类别归纳用 columns（2-4 栏）；
  数字对比、多方案对照用 table 或 compare；朴素叙述才用 content（2-6 条，杜绝一两条的空页）；
- 要点用「引导词｜正文」写法（如 "核心思想｜把打分并入同一模型"），引导词 ≤6 字会加粗着色；
- 标题 ≤20 字；全文禁止破折号（— – ——）；每页信息一屏放满但不塞爆，细节写进 notes；
- 图文并茂：可用配图尽量都用上，每张 figure 页 caption ≥15 字讲清看什么；
- 第 1 页 cover、第 2 页 toc、每章 section 过渡、最后 closing；不要出现 schema 外的字段。
"""

OUTLINE_SYSTEM = """\
你是学术分享的讲者，为论文分享 PPT 设计大纲。
只输出 JSON：{"sections": [{"title": "章节短标题", "points": ["该章节要讲的要点", ...]}]}
章节 3-5 个；标题 ≤20 字；先讲问题与动机，再讲方法直觉，再讲证据，最后讲启发。
"""

SLIDES_SYSTEM = (
    """\
你是学术分享 PPT 的内容设计者，把论文材料按大纲转成完整的幻灯片甲板 JSON。
风格：每页信息克制、口语化短句、讲逻辑不堆术语；配图页用 caption 告诉观众看图中哪里。
"""
    + DECK_JSON_SPEC
)

FIX_SYSTEM = (
    """\
你是 PPT 修订者。下面给出甲板 JSON 与未通过的规范检查项，请修复后输出完整甲板 JSON。
只改有问题的地方，保持其余内容不变。
"""
    + DECK_JSON_SPEC
)

VISUAL_SYSTEM = """\
你是幻灯片视觉审查者，逐页检查随消息附上的 PPT 渲染图。
只输出 JSON：{"ok": true/false, "issues": [{"slide": 页码(从1起), "problem": "问题", "fix": "怎么改"}]}
检查项：文字是否溢出或被截断；图片是否变形、过小、图中文字是否看得清；
版面是否拥挤或大片留白；标题与正文层级是否清晰。轻微瑕疵可接受，只报会影响观感的问题。
"""


def _params(ctx: ActionContext) -> dict[str, Any]:
    params = (ctx.checkpoint or {}).get("params")
    return params if isinstance(params, dict) else {}


def _extract_json(content: str) -> Any:
    start, end = content.find("{"), content.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object found")
    return json.loads(content[start : end + 1])


async def _complete_json(
    ctx: ActionContext, *, system: str, user: str, validate, images: list[bytes] | None = None
) -> Any:
    last_error: Exception | None = None
    for _ in range(_MAX_JSON_ATTEMPTS):
        result = await ctx.llm.complete(
            _STAGE,
            [Message(role="system", content=system), Message(role="user", content=user)],
            images=images,
            user_id=ctx.run.created_by,
            project_id=ctx.run.project_id,
            voyage_id=ctx.run.id,
        )
        try:
            return validate(_extract_json(result.content))
        except (ValueError, TypeError, KeyError, json.JSONDecodeError) as e:
            last_error = e
    raise ValueError(f"LLM 连续输出非法 JSON：{last_error}")


async def _load_papers(ctx: ActionContext) -> list[tuple[Paper, str | None]]:
    """按 params.paper_ids 加载库内论文，返回 (Paper, 本方向库版 wiki) 对（保持传入顺序）。"""
    ids = [uuid.UUID(str(i)) for i in _params(ctx).get("paper_ids") or []]
    async with get_sessionmaker()() as session:
        library_ids = await get_source_library_ids(session, ctx.run.project_id)
        rows = (
            dedupe_member_rows(
                (
                    await session.execute(
                        member_papers_stmt(library_ids).where(Paper.id.in_(ids))
                    )
                ).all()
            )
            if library_ids
            else []
        )
    by_id = {p.id: (p, m.wiki_content) for p, m in rows}
    ordered = [by_id[i] for i in ids if i in by_id]
    if not ordered:
        raise ValueError("presentation: no papers found for given paper_ids")
    return ordered


def _figure_catalog(papers: list[Paper]) -> tuple[list[dict[str, Any]], dict[int, bytes]]:
    """跨论文统一编号的配图目录：LLM 用全局 ref 引用，build 用 ref 取 bytes。"""
    catalog: list[dict[str, Any]] = []
    blobs: dict[int, bytes] = {}
    for paper in papers:
        for fig, data in important_figures_with_bytes(paper, limit=4):
            ref = len(catalog)
            catalog.append(
                {
                    "ref": ref,
                    "paper": paper.title[:40],
                    "kind": fig.get("kind") or "other",
                    "caption": fig.get("caption") or "",
                }
            )
            blobs[ref] = data
    return catalog, blobs


# ---- ① 取材（确定性） ----


@register("present.collect")
async def present_collect(ctx: ActionContext, params: dict[str, Any]) -> dict[str, Any]:
    pairs = await _load_papers(ctx)
    papers = [p for p, _ in pairs]
    mode = str(_params(ctx).get("mode") or ("single" if len(papers) == 1 else "survey"))
    cap = _SINGLE_BODY_CHARS if mode == "single" else _SURVEY_BODY_CHARS
    materials = []
    for p, wiki in pairs:
        authors = "、".join(a.get("name", "") for a in (p.authors or []) if isinstance(a, dict))
        materials.append(
            {
                "title": p.title,
                "authors": authors[:120],
                "venue": f"{p.venue or ''} {p.year or ''}".strip(),
                "tldr": p.tldr or "",
                "body": (wiki or p.abstract or "")[:cap],
            }
        )
    catalog, _ = _figure_catalog(papers)
    ctx.checkpoint["present_ctx"] = {"mode": mode, "materials": materials, "figures": catalog}
    return {"papers": len(materials), "figures": len(catalog), "mode": mode}


# ---- ② 大纲（LLM，注入点 present.outline） ----


@register("present.outline")
async def present_outline(ctx: ActionContext, params: dict[str, Any]) -> dict[str, Any]:
    pctx = ctx.checkpoint.get("present_ctx") or {}
    mode = pctx.get("mode", "single")
    task = (
        "为下面这一篇论文设计单篇分享 PPT 的大纲。"
        if mode == "single"
        else "为下面多篇论文设计一场主题梳理 PPT 的大纲：按主题线组织，不要逐篇流水账，"
        "要有跨论文的对比与共性提炼。"
    )
    user = task + "\n\n" + json.dumps(pctx.get("materials"), ensure_ascii=False)
    if notes := _params(ctx).get("notes"):
        user += f"\n\n讲者备注（需照顾的侧重点）：{notes}"

    def validate(data: Any) -> list[dict[str, Any]]:
        sections = data.get("sections") if isinstance(data, dict) else None
        if not isinstance(sections, list) or not sections:
            raise ValueError('expected {"sections": [...]}')
        return [
            {"title": str(s["title"]), "points": [str(x) for x in s.get("points") or []]}
            for s in sections
            if isinstance(s, dict) and s.get("title")
        ]

    sections = await _complete_json(
        ctx,
        system=OUTLINE_SYSTEM + ctx.skill_guidance("present.outline"),
        user=user,
        validate=validate,
    )
    ctx.checkpoint["present_outline"] = sections
    return {"sections": len(sections), "titles": [s["title"] for s in sections]}


# ---- ③ 甲板内容（LLM，注入点 present.slides） ----


@register("present.slides")
async def present_slides(ctx: ActionContext, params: dict[str, Any]) -> dict[str, Any]:
    pctx = ctx.checkpoint.get("present_ctx") or {}
    user = (
        f"分享模式：{pctx.get('mode')}\n"
        f"大纲：{json.dumps(ctx.checkpoint.get('present_outline'), ensure_ascii=False)}\n"
        f"可用配图目录（figure_index 用 ref 值）：{json.dumps(pctx.get('figures'), ensure_ascii=False)}\n"
        f"论文材料：{json.dumps(pctx.get('materials'), ensure_ascii=False)}"
    )

    def validate(data: Any) -> dict[str, Any]:
        return DeckSpec.model_validate(data).model_dump()

    deck = await _complete_json(
        ctx,
        system=SLIDES_SYSTEM + ctx.skill_guidance("present.slides"),
        user=user,
        validate=validate,
    )
    ctx.checkpoint["present_deck"] = deck
    return {"slides": len(deck["slides"]), "deck_title": deck["title"]}


# ---- ④ 渲染 + 反馈迭代（确定性构建，LLM 只负责修） ----


async def _fix_deck(ctx: ActionContext, deck: dict[str, Any], diagnosis: str) -> dict[str, Any]:
    def validate(data: Any) -> dict[str, Any]:
        return DeckSpec.model_validate(data).model_dump()

    user = f"甲板 JSON：\n{json.dumps(deck, ensure_ascii=False)}\n\n未通过的检查：\n{diagnosis}"
    return await _complete_json(ctx, system=FIX_SYSTEM, user=user, validate=validate)


@register("present.build")
async def present_build(ctx: ActionContext, params: dict[str, Any]) -> dict[str, Any]:
    deck = ctx.checkpoint.get("present_deck")
    if not deck:
        raise ValueError("present.build requires present_deck in checkpoint")
    papers = [p for p, _ in await _load_papers(ctx)]
    _, blobs = _figure_catalog(papers)

    # ①文本规范迭代：确定性校验 → LLM 修
    text_rounds = 0
    spec = DeckSpec.model_validate(deck)
    for _ in range(_FIX_ROUNDS):
        errors = validate_deck_spec(spec, figure_indices=set(blobs))
        if not errors:
            break
        text_rounds += 1
        deck = await _fix_deck(ctx, spec.model_dump(), "\n".join(f"- {e}" for e in errors))
        spec = DeckSpec.model_validate(deck)
    remaining = validate_deck_spec(spec, figure_indices=set(blobs))

    pptx = build_deck(spec, blobs)

    # ②视觉反馈迭代：soffice 渲染成图 → VLM 审 → LLM 修（无 soffice 降级跳过）
    visual_rounds, visual_issues = 0, []
    if soffice_available():
        for _ in range(_FIX_ROUNDS):
            images = render_slide_images(pptx)
            if not images:
                break

            def validate(data: Any) -> dict[str, Any]:
                if not isinstance(data, dict) or not isinstance(data.get("ok"), bool):
                    raise ValueError("expected {ok, issues}")
                return {"ok": data["ok"], "issues": data.get("issues") or []}

            review = await _complete_json(
                ctx,
                system=VISUAL_SYSTEM,
                user=f"共 {len(images)} 页渲染图，按顺序对应第 1-{len(images)} 页。",
                validate=validate,
                images=images[:8],
            )
            if review["ok"] or not review["issues"]:
                break
            visual_rounds += 1
            visual_issues = review["issues"]
            diagnosis = "\n".join(
                f"- 第 {i.get('slide')} 页：{i.get('problem')}（建议：{i.get('fix')}）"
                for i in review["issues"]
                if isinstance(i, dict)
            )
            deck = await _fix_deck(ctx, spec.model_dump(), "视觉审查未通过：\n" + diagnosis)
            spec = DeckSpec.model_validate(deck)
            pptx = build_deck(spec, blobs)

    out_dir = Path(get_settings().data_dir) / "presentations"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{ctx.run.id}.pptx"
    path.write_bytes(pptx)
    ctx.checkpoint["present_deck"] = spec.model_dump()
    ctx.checkpoint["presentation"] = {
        "path": str(path),
        "slides": len(spec.slides),
        "deck_title": spec.title,
        "text_fix_rounds": text_rounds,
        "visual_fix_rounds": visual_rounds,
        "render_feedback": "soffice" if soffice_available() else "skipped",
    }
    return {
        "path": str(path),
        "slides": len(spec.slides),
        "size_kb": len(pptx) // 1024,
        "text_fix_rounds": text_rounds,
        "visual_fix_rounds": visual_rounds,
        "render_feedback": "soffice" if soffice_available() else "skipped",
        "unresolved_text_issues": remaining,
        "last_visual_issues": visual_issues,
    }
