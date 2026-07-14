"""idea forge / review 动作（Voyage kinds ``idea_forge`` / ``idea_review`` 的固定计划执行体）。

forge 流水线（docs/api-m3.md §1）：
    forge.read_context → forge.gap_analysis → forge.generate →
    forge.score → forge.dedup → forge.persist
review 流水线（docs/api-m3.md §3）：
    review.pair → review.debate → review.summarize

健壮性约定（与 actions_wiki 一致）：
- 逐 idea / 逐场辩论独立 try/except，单条失败不打断批处理，observation 汇总 failed；
- checkpoint 幂等：gap/候选/评分/去重结果与已完成对局都记入 checkpoint，
  断点续跑不重复调 LLM、persist 不重复入库；
- 判断性任务（gap 分析/生成/打分/辩论/裁判）走 core/llm 路由，其余全为确定性代码。
"""

import asyncio
import json
import math
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.voyage.actions import ActionContext, register
from app.core.db import get_sessionmaker
from app.core.llm.base import Message
from app.models.activity import Activity
from app.models.idea import Idea
from app.models.paper import Concept, Paper
from app.models.project import Project
from app.models.review import ReviewMessage, ReviewSession
from app.services.review import (
    DEFAULT_PERSONAS,
    elo_update,
    human_comments,
    serialize_message,
)

DEFAULT_FORGE_KNOBS: dict[str, Any] = {
    "num_ideas": 8,
    "dedup_threshold": 0.85,
    "max_context_papers": 20,
}
DEFAULT_ROUNDS = 2

_MAX_JSON_ATTEMPTS = 3  # 首次 + 重试 2 次
_CONTEXT_CHARS = 12000  # 知识库上下文注入 prompt 的总长上限
_WIKI_EXCERPT_CHARS = 800
_RERANK_CONFIRM_SCORE = 0.5  # 余弦超阈后 rerank 复核的确认线
_SCORE_DIMS = ("novelty", "feasibility", "operability", "impact")

GAP_SYSTEM_PROMPT = """\
你是 Idea Forge 的研究空白分析师，基于项目知识库综述找出值得研究的空白。
只输出一个 JSON 对象，不要输出任何其他文字或 Markdown 代码块，格式：
{"gaps": [{"title": "空白标题", "description": "为什么是空白、机会在哪"}]}
给出 3-6 个研究空白。
"""

GENERATE_SYSTEM_PROMPT = """\
你是 Idea Forge 的想法生成器，围绕研究空白提出具体可执行的研究想法。
只输出一个 JSON 对象，不要输出任何其他文字或 Markdown 代码块，格式：
{"ideas": [{"title": "想法标题", "summary": "一句话概述", "motivation": "动机",
"method": "方法概述", "experiments": "预期实验", "risks": "风险"}]}
"""

SCORE_SYSTEM_PROMPT = """\
你是 Idea Forge 的评审员，对一个研究想法做四维独立打分（各维 0-10）。
只输出一个 JSON 对象，不要输出任何其他文字或 Markdown 代码块，格式：
{"novelty": 0-10, "feasibility": 0-10, "operability": 0-10, "impact": 0-10,
 "rationale": {"novelty": "理由", "feasibility": "理由", "operability": "理由", "impact": "理由"}}
"""

JUDGE_SYSTEM_PROMPT = """\
你是评审人设「{name}」（立场：{stance}），担任这场科学辩论的裁判。
基于双方全部发言判定哪个想法更值得投入研究。
只输出一个 JSON 对象，不要输出任何其他文字，格式：
{{"winner": "a" 或 "b", "reason": "判定理由"}}
"""


# ---- 公共小件 ----


def _params(ctx: ActionContext) -> dict[str, Any]:
    params = (ctx.checkpoint or {}).get("params")
    return params if isinstance(params, dict) else {}


def resolve_forge_knobs(raw: Any) -> dict[str, Any]:
    knobs = dict(DEFAULT_FORGE_KNOBS)
    if isinstance(raw, dict):
        for key in DEFAULT_FORGE_KNOBS:
            if raw.get(key) is not None:
                knobs[key] = raw[key]
    return knobs


def _forge_knobs(ctx: ActionContext) -> dict[str, Any]:
    return resolve_forge_knobs(_params(ctx).get("knobs"))


async def _get_project(session: AsyncSession, ctx: ActionContext) -> Project:
    project = await session.get(Project, ctx.run.project_id)
    if project is None:
        raise ValueError(f"project not found: {ctx.run.project_id}")
    return project


def _statement(project: Project) -> str:
    definition = project.definition if isinstance(project.definition, dict) else {}
    return str(definition.get("statement") or project.name)


def _extract_json(content: str) -> Any:
    start = content.find("{")
    end = content.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object found")
    return json.loads(content[start : end + 1])


async def _complete_json(
    ctx: ActionContext, *, stage: str, system: str, user: str, validate
) -> Any:
    """LLM JSON 请求：解析/校验失败重试（共 _MAX_JSON_ATTEMPTS 次），仍失败抛 ValueError。"""
    last_error: Exception | None = None
    for _attempt in range(_MAX_JSON_ATTEMPTS):
        result = await ctx.llm.complete(
            stage,
            [Message(role="system", content=system), Message(role="user", content=user)],
            user_id=ctx.run.created_by,
            project_id=ctx.run.project_id,
            voyage_id=ctx.run.id,
        )
        try:
            return validate(_extract_json(result.content))
        except (ValueError, TypeError, KeyError, json.JSONDecodeError) as e:
            last_error = e
    raise ValueError(f"LLM 连续输出非法 JSON：{last_error}")


def cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    norm_a = math.sqrt(sum(x * x for x in a)) or 1.0
    norm_b = math.sqrt(sum(x * x for x in b)) or 1.0
    return dot / (norm_a * norm_b)


def _idea_text(title: str, summary: str | None) -> str:
    return f"{title}\n{summary or ''}"[:2000]


def _idea_markdown(item: dict[str, Any]) -> str:
    sections = (
        ("动机", "motivation"),
        ("方法概述", "method"),
        ("预期实验", "experiments"),
        ("风险", "risks"),
    )
    parts = [f"## {zh}\n\n{str(item.get(key) or '（待补充）').strip()}" for zh, key in sections]
    return "\n\n".join(parts)


# ---- forge 1. 读取知识库上下文 ----


@register("forge.read_context")
async def forge_read_context(ctx: ActionContext, params: dict[str, Any]) -> dict[str, Any]:
    knobs = _forge_knobs(ctx)
    async with get_sessionmaker()() as session:
        project = await _get_project(session, ctx)
        papers = (
            (
                await session.execute(
                    select(Paper)
                    .where(
                        Paper.project_id == project.id,
                        Paper.status.in_(("compiled", "included")),
                        Paper.wiki_content.is_not(None),
                    )
                    .order_by(Paper.relevance_score.desc().nulls_last(), Paper.created_at)
                    .limit(int(knobs["max_context_papers"]))
                )
            )
            .scalars()
            .all()
        )
        concept_names = (
            (
                await session.execute(
                    select(Concept.name)
                    .where(Concept.project_id == project.id)
                    .order_by(Concept.name)
                    .limit(100)
                )
            )
            .scalars()
            .all()
        )

    parts = []
    for paper in papers:
        excerpt = (paper.wiki_content or "")[:_WIKI_EXCERPT_CHARS]
        parts.append(f"### {paper.title}\nTL;DR：{paper.tldr or '（无）'}\n{excerpt}")
    context_text = "\n\n".join(parts)[:_CONTEXT_CHARS] or "（知识库为空）"

    ctx.checkpoint["forge_context"] = {
        "paper_ids": [str(p.id) for p in papers],
        "concepts": list(concept_names),
        "text": context_text,
    }
    return {"papers": len(papers), "concepts": len(concept_names)}


def _context(ctx: ActionContext) -> dict[str, Any]:
    context = ctx.checkpoint.get("forge_context")
    return context if isinstance(context, dict) else {}


def _context_prompt(ctx: ActionContext, statement: str) -> str:
    context = _context(ctx)
    concepts = "、".join(context.get("concepts") or []) or "（无）"
    return (
        f"研究方向：{statement}\n"
        f"知识库概念：{concepts}\n"
        f"知识库综述（compiled wiki 摘要）：\n{context.get('text') or '（知识库为空）'}"
    )


# ---- forge 2. gap 分析（LLM stage=forge） ----


@register("forge.gap_analysis")
async def forge_gap_analysis(ctx: ActionContext, params: dict[str, Any]) -> dict[str, Any]:
    if isinstance(ctx.checkpoint.get("forge_gaps"), list):  # 断点幂等
        return {"gaps": len(ctx.checkpoint["forge_gaps"]), "skipped": True}

    async with get_sessionmaker()() as session:
        project = await _get_project(session, ctx)
        statement = _statement(project)

    def validate(data: Any) -> list[dict[str, str]]:
        gaps = data.get("gaps") if isinstance(data, dict) else None
        if not isinstance(gaps, list) or not gaps:
            raise ValueError('expected {"gaps": [...]}')
        normalized = []
        for gap in gaps:
            if not isinstance(gap, dict) or not gap.get("title"):
                raise ValueError("gap missing title")
            normalized.append(
                {"title": str(gap["title"]), "description": str(gap.get("description") or "")}
            )
        return normalized

    gaps = await _complete_json(
        ctx,
        stage="forge",
        system=GAP_SYSTEM_PROMPT,
        user=_context_prompt(ctx, statement),
        validate=validate,
    )
    ctx.checkpoint["forge_gaps"] = gaps
    return {"gaps": len(gaps), "titles": [g["title"] for g in gaps]}


# ---- forge 3. 生成候选（LLM stage=forge） ----


@register("forge.generate")
async def forge_generate(ctx: ActionContext, params: dict[str, Any]) -> dict[str, Any]:
    if isinstance(ctx.checkpoint.get("forge_candidates"), list):  # 断点幂等
        return {"generated": len(ctx.checkpoint["forge_candidates"]), "skipped": True}
    knobs = _forge_knobs(ctx)
    num_ideas = int(knobs["num_ideas"])

    async with get_sessionmaker()() as session:
        project = await _get_project(session, ctx)
        statement = _statement(project)

    gaps = ctx.checkpoint.get("forge_gaps") or []
    user_prompt = (
        f"{_context_prompt(ctx, statement)}\n"
        f"研究空白清单：{json.dumps(gaps, ensure_ascii=False)}\n"
        f"请围绕以上空白生成 {num_ideas} 个候选想法。"
    )

    def validate(data: Any) -> list[dict[str, Any]]:
        ideas = data.get("ideas") if isinstance(data, dict) else None
        if not isinstance(ideas, list) or not ideas:
            raise ValueError('expected {"ideas": [...]}')
        normalized = []
        for item in ideas:
            if not isinstance(item, dict) or not str(item.get("title") or "").strip():
                raise ValueError("idea missing title")
            normalized.append(
                {
                    "title": str(item["title"]).strip()[:512],
                    "summary": str(item.get("summary") or "").strip() or None,
                    "content": _idea_markdown(item),
                    "scores": None,
                    "score_rationale": None,
                    "duplicate": False,
                }
            )
        return normalized[:num_ideas]

    candidates = await _complete_json(
        ctx, stage="forge", system=GENERATE_SYSTEM_PROMPT, user=user_prompt, validate=validate
    )
    ctx.checkpoint["forge_candidates"] = candidates
    return {"requested": num_ideas, "generated": len(candidates)}


# ---- forge 4. 四维打分（LLM stage=forge，逐 idea 独立调用） ----


def _validate_scores(data: Any) -> tuple[dict[str, float], dict[str, str]]:
    if not isinstance(data, dict):
        raise ValueError("scores payload is not an object")
    scores: dict[str, float] = {}
    for dim in _SCORE_DIMS:
        value = data.get(dim)
        if not isinstance(value, int | float):
            raise ValueError(f"missing numeric score: {dim}")
        scores[dim] = min(10.0, max(0.0, float(value)))
    raw_rationale = data.get("rationale")
    rationale = (
        {dim: str(raw_rationale.get(dim) or "") for dim in _SCORE_DIMS}
        if isinstance(raw_rationale, dict)
        else {}
    )
    return scores, rationale


@register("forge.score")
async def forge_score(ctx: ActionContext, params: dict[str, Any]) -> dict[str, Any]:
    candidates: list[dict[str, Any]] = list(ctx.checkpoint.get("forge_candidates") or [])
    scored = 0
    skipped = 0
    failed: list[dict[str, str]] = []

    async with get_sessionmaker()() as session:
        project = await _get_project(session, ctx)
        statement = _statement(project)

    for cand in candidates:
        if isinstance(cand.get("scores"), dict):  # 断点幂等：已评分不重复调 LLM
            skipped += 1
            continue
        user_prompt = (
            f"研究方向：{statement}\n"
            f"想法标题：{cand['title']}\n"
            f"一句话概述：{cand.get('summary') or '（无）'}\n"
            f"详情：\n{cand.get('content') or ''}"
        )
        try:
            scores, rationale = await _complete_json(
                ctx,
                stage="forge",
                system=SCORE_SYSTEM_PROMPT,
                user=user_prompt,
                validate=_validate_scores,
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 — 逐 idea 失败隔离（无评分入库）
            failed.append({"title": str(cand["title"]), "error": f"{type(e).__name__}: {e}"})
            continue
        cand["scores"] = scores
        cand["score_rationale"] = rationale
        scored += 1

    ctx.checkpoint["forge_candidates"] = candidates
    return {
        "processed": len(candidates),
        "succeeded": scored,
        "skipped": skipped,
        "failed": failed,
    }


# ---- forge 5. 语义去重（embedding 余弦 + rerank 复核） ----


@register("forge.dedup")
async def forge_dedup(ctx: ActionContext, params: dict[str, Any]) -> dict[str, Any]:
    knobs = _forge_knobs(ctx)
    threshold = float(knobs["dedup_threshold"])
    candidates: list[dict[str, Any]] = list(ctx.checkpoint.get("forge_candidates") or [])
    if ctx.checkpoint.get("forge_dedup_done"):  # 断点幂等
        dropped = [c for c in candidates if c.get("duplicate")]
        return {"candidates": len(candidates), "dropped": len(dropped), "skipped": True}
    if not candidates:
        ctx.checkpoint["forge_dedup_done"] = True
        return {"candidates": 0, "dropped": 0}

    cand_texts = [_idea_text(c["title"], c.get("summary")) for c in candidates]
    try:
        cand_vectors = await ctx.llm.embed(
            cand_texts,
            user_id=ctx.run.created_by,
            project_id=ctx.run.project_id,
            voyage_id=ctx.run.id,
        )
    except NotImplementedError:
        # embedding 路由不可用：跳过去重（记录原因），全部候选放行
        ctx.checkpoint["forge_dedup_done"] = True
        return {"candidates": len(candidates), "dropped": 0, "skipped_reason": "no embedding"}

    # 库内既有 idea：无向量的现场补嵌并落库
    async with get_sessionmaker()() as session:
        existing = (
            (
                await session.execute(
                    select(Idea).where(
                        Idea.project_id == ctx.run.project_id, Idea.status != "rejected"
                    )
                )
            )
            .scalars()
            .all()
        )
        pending = [i for i in existing if i.embedding is None]
        if pending:
            try:
                vectors = await ctx.llm.embed(
                    [_idea_text(i.title, i.summary) for i in pending],
                    user_id=ctx.run.created_by,
                    project_id=ctx.run.project_id,
                    voyage_id=ctx.run.id,
                )
                for idea, vector in zip(pending, vectors, strict=True):
                    idea.embedding = vector
                await session.commit()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — 既有 idea 补嵌失败则跳过比对
                pass
        existing_entries = [
            (f"库内想法「{i.title}」", _idea_text(i.title, i.summary), list(i.embedding))
            for i in existing
            if i.embedding is not None
        ]

    async def confirmed_duplicate(text: str, other_text: str, cosine: float) -> tuple[bool, float]:
        """余弦超阈的对用 rerank 复核；rerank 不可用时以余弦为准。"""
        try:
            ranked = await ctx.llm.rerank(
                text,
                [other_text],
                user_id=ctx.run.created_by,
                project_id=ctx.run.project_id,
                voyage_id=ctx.run.id,
            )
            score = float(ranked[0][1]) if ranked else 0.0
            return score >= _RERANK_CONFIRM_SCORE, score
        except NotImplementedError:
            return True, cosine
        except Exception:  # noqa: BLE001 — rerank 异常降级为余弦判定
            return True, cosine

    dropped: list[dict[str, Any]] = []
    kept: list[tuple[str, str, list[float]]] = list(existing_entries)
    for cand, text, vector in zip(candidates, cand_texts, cand_vectors, strict=True):
        duplicate_of: str | None = None
        for label, other_text, other_vector in kept:
            cosine = cosine_similarity(vector, other_vector)
            if cosine <= threshold:
                continue
            is_dup, rerank_score = await confirmed_duplicate(text, other_text, cosine)
            if is_dup:
                duplicate_of = label
                dropped.append(
                    {
                        "title": cand["title"],
                        "duplicate_of": label,
                        "cosine": round(cosine, 4),
                        "rerank_score": round(rerank_score, 4),
                    }
                )
                break
        if duplicate_of is None:
            kept.append((f"本批候选「{cand['title']}」", text, vector))
        cand["duplicate"] = duplicate_of is not None

    ctx.checkpoint["forge_candidates"] = candidates
    ctx.checkpoint["forge_dedup_done"] = True
    return {
        "candidates": len(candidates),
        "existing_compared": len(existing_entries),
        "dropped": len(dropped),
        "dropped_detail": dropped,
        "threshold": threshold,
    }


# ---- forge 6. 入库候选池 ----


@register("forge.persist")
async def forge_persist(ctx: ActionContext, params: dict[str, Any]) -> dict[str, Any]:
    if isinstance(ctx.checkpoint.get("forge_inserted_ids"), list):  # 断点幂等：不重复入库
        return {"inserted": len(ctx.checkpoint["forge_inserted_ids"]), "skipped": True}
    candidates: list[dict[str, Any]] = list(ctx.checkpoint.get("forge_candidates") or [])
    survivors = [c for c in candidates if not c.get("duplicate")]
    parent_paper_ids = list(_context(ctx).get("paper_ids") or [])

    embeddings: list[list[float] | None] = [None] * len(survivors)
    if survivors:
        try:
            embeddings = list(
                await ctx.llm.embed(
                    [_idea_text(c["title"], c.get("summary")) for c in survivors],
                    user_id=ctx.run.created_by,
                    project_id=ctx.run.project_id,
                    voyage_id=ctx.run.id,
                )
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — 嵌入失败不阻塞入库（含 NotImplementedError）
            embeddings = [None] * len(survivors)

    inserted_ids: list[str] = []
    async with get_sessionmaker()() as session:
        for cand, vector in zip(survivors, embeddings, strict=True):
            idea = Idea(
                project_id=ctx.run.project_id,
                title=cand["title"],
                summary=cand.get("summary"),
                content=cand.get("content"),
                scores=cand.get("scores"),
                score_rationale=cand.get("score_rationale"),
                status="candidate",
                parent_paper_ids=parent_paper_ids,
                embedding=vector,
            )
            session.add(idea)
            await session.flush()
            inserted_ids.append(str(idea.id))
        session.add(
            Activity(
                project_id=ctx.run.project_id,
                actor="agent:idea-forge",
                kind="forge.completed",
                message=f"Idea Forge 完成：{len(inserted_ids)} 个候选想法入库",
                payload={
                    "voyage_id": str(ctx.run.id),
                    "inserted": len(inserted_ids),
                    "dropped_duplicates": len(candidates) - len(survivors),
                },
            )
        )
        await session.commit()

    ctx.checkpoint["forge_inserted_ids"] = inserted_ids
    return {
        "inserted": len(inserted_ids),
        "dropped_duplicates": len(candidates) - len(survivors),
        "idea_ids": inserted_ids,
    }


# ---- review 1. 配对（Swiss：按 Elo 排序相邻配对） ----


def _personas(ctx: ActionContext) -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    """人设分工：[0]=正方 [1]=反方 [2]=裁判；不足三个用默认人设补齐。"""
    raw = _params(ctx).get("personas")
    personas = [p for p in raw if isinstance(p, dict) and p.get("name")] if raw else []
    merged = (personas + DEFAULT_PERSONAS[len(personas) :])[:3]
    return merged[0], merged[1], merged[2]


def _rounds(ctx: ActionContext) -> int:
    try:
        return max(1, min(5, int(_params(ctx).get("rounds") or DEFAULT_ROUNDS)))
    except (TypeError, ValueError):
        return DEFAULT_ROUNDS


@register("review.pair")
async def review_pair(ctx: ActionContext, params: dict[str, Any]) -> dict[str, Any]:
    if isinstance(ctx.checkpoint.get("review_pairs"), list):  # 断点幂等
        return {"pairs": len(ctx.checkpoint["review_pairs"]), "skipped": True}
    explicit_ids = _params(ctx).get("idea_ids")

    async with get_sessionmaker()() as session:
        stmt = select(Idea).where(Idea.project_id == ctx.run.project_id)
        if explicit_ids:
            stmt = stmt.where(Idea.id.in_([uuid.UUID(str(i)) for i in explicit_ids]))
        else:
            stmt = stmt.where(Idea.status.in_(("candidate", "under_review")))
        stmt = stmt.order_by(Idea.elo_rating.desc(), Idea.created_at)
        ideas = (await session.execute(stmt)).scalars().all()

        status_changed: list[Idea] = []
        for idea in ideas:
            if idea.status == "candidate":
                idea.status = "under_review"
                status_changed.append(idea)
        await session.commit()
        for idea in status_changed:
            await ctx.notify(
                {"type": "idea.status", "idea_id": str(idea.id), "status": idea.status}
            )
        ordered_ids = [str(i.id) for i in ideas]

    pairs = [[ordered_ids[i], ordered_ids[i + 1]] for i in range(0, len(ordered_ids) - 1, 2)]
    bye = ordered_ids[-1] if len(ordered_ids) % 2 == 1 else None
    ctx.checkpoint["review_pairs"] = pairs
    return {"participants": len(ordered_ids), "pairs": len(pairs), "bye": bye}


# ---- review 2. 科学辩论 + 裁判判定 + Elo 更新 ----


def _persona_system(persona: dict[str, str], side: str, idea_title: str) -> str:
    return (
        f"你是评审人设「{persona['name']}」，立场：{persona.get('stance') or ''}。\n"
        f"你在一场关于两个研究想法的科学辩论中担任{side}，为想法「{idea_title}」辩护，"
        "同时指出对方想法的不足。\n"
        "请用中文发言，观点犀利、有理有据，直接输出发言内容。"
    )


def _debate_brief(label: str, idea: Idea) -> str:
    return (
        f"想法 {label}：{idea.title}\n"
        f"概述：{idea.summary or '（无）'}\n"
        f"详情：\n{(idea.content or '')[:2000]}"
    )


def _human_block(label: str, comments: list[str]) -> str:
    if not comments:
        return f"想法 {label} 的人类评审意见：（暂无）"
    return f"想法 {label} 的人类评审意见：\n" + "\n".join(f"- {c}" for c in comments)


async def _add_agent_message(
    session: AsyncSession,
    ctx: ActionContext,
    review_session: ReviewSession,
    *,
    author_name: str,
    content: str,
    round_no: int,
) -> ReviewMessage:
    message = ReviewMessage(
        session_id=review_session.id,
        author_type="agent",
        author_name=author_name,
        content=content,
        round=round_no,
    )
    session.add(message)
    await session.commit()
    await session.refresh(message)
    await ctx.notify(
        {
            "type": "review.message",
            "session_id": str(review_session.id),
            "project_id": str(ctx.run.project_id),
            "message": serialize_message(message),
        }
    )
    return message


def _validate_verdict(data: Any) -> dict[str, str]:
    if not isinstance(data, dict) or data.get("winner") not in ("a", "b"):
        raise ValueError('expected {"winner": "a"|"b", "reason": ...}')
    return {"winner": str(data["winner"]), "reason": str(data.get("reason") or "")}


async def _run_match(
    ctx: ActionContext, session: AsyncSession, idea_a: Idea, idea_b: Idea, match_no: int
) -> dict[str, Any]:
    pro, con, judge = _personas(ctx)
    rounds = _rounds(ctx)

    review_session = ReviewSession(
        target_type="idea_match",
        target_id=idea_a.id,
        payload={"idea_a": str(idea_a.id), "idea_b": str(idea_b.id), "round": match_no},
    )
    session.add(review_session)
    await session.commit()
    await session.refresh(review_session)

    comments_a = await human_comments(session, idea_a.id)
    comments_b = await human_comments(session, idea_b.id)
    base_context = (
        f"{_debate_brief('A', idea_a)}\n\n{_debate_brief('B', idea_b)}\n\n"
        f"{_human_block('A', comments_a)}\n{_human_block('B', comments_b)}"
    )

    transcript: list[str] = []
    round_no = 0
    for debate_round in range(1, rounds + 1):
        for persona, side, idea in ((pro, "正方", idea_a), (con, "反方", idea_b)):
            round_no += 1
            transcript_text = "\n\n".join(transcript) or "（辩论刚开始）"
            user_prompt = (
                f"{base_context}\n\n此前发言：\n{transcript_text}\n\n"
                f"第 {debate_round} 轮，请发表你的{side}观点。"
            )
            result = await ctx.llm.complete(
                "debate",
                [
                    Message(role="system", content=_persona_system(persona, side, idea.title)),
                    Message(role="user", content=user_prompt),
                ],
                user_id=ctx.run.created_by,
                project_id=ctx.run.project_id,
                voyage_id=ctx.run.id,
            )
            content = result.content.strip() or "（无发言）"
            await _add_agent_message(
                session,
                ctx,
                review_session,
                author_name=str(persona["name"]),
                content=content,
                round_no=round_no,
            )
            transcript.append(f"{persona['name']}（{side}）：{content}")

    # 裁判判定（JSON 校验重试）
    verdict = await _complete_json(
        ctx,
        stage="debate",
        system=JUDGE_SYSTEM_PROMPT.format(name=judge["name"], stance=judge.get("stance") or ""),
        user=f"{base_context}\n\n辩论全文：\n" + "\n\n".join(transcript),
        validate=_validate_verdict,
    )
    round_no += 1
    winner_idea = idea_a if verdict["winner"] == "a" else idea_b
    await _add_agent_message(
        session,
        ctx,
        review_session,
        author_name=str(judge["name"]),
        content=f"判定胜者：{verdict['winner']}（{winner_idea.title}）。理由:{verdict['reason']}",
        round_no=round_no,
    )

    # Elo 更新（K=32）+ 战绩累计
    new_a, new_b = elo_update(idea_a.elo_rating, idea_b.elo_rating, verdict["winner"])
    idea_a.elo_rating = new_a
    idea_b.elo_rating = new_b
    idea_a.matches += 1
    idea_b.matches += 1
    winner_idea.wins += 1
    review_session.payload = dict(review_session.payload or {}) | {
        "winner": verdict["winner"],
        "reason": verdict["reason"],
    }
    review_session.status = "closed"
    await session.commit()

    return {
        "session_id": str(review_session.id),
        "idea_a": str(idea_a.id),
        "idea_b": str(idea_b.id),
        "winner": verdict["winner"],
        "elo": {str(idea_a.id): round(new_a, 2), str(idea_b.id): round(new_b, 2)},
    }


@register("review.debate")
async def review_debate(ctx: ActionContext, params: dict[str, Any]) -> dict[str, Any]:
    pairs: list[list[str]] = list(ctx.checkpoint.get("review_pairs") or [])
    results: list[dict[str, Any]] = list(ctx.checkpoint.get("review_results") or [])
    done = {(r["idea_a"], r["idea_b"]) for r in results}
    failed: list[dict[str, str]] = []

    async with get_sessionmaker()() as session:
        for match_no, pair in enumerate(pairs, start=1):
            key = (str(pair[0]), str(pair[1]))
            if key in done:  # 断点幂等：已完成对局不重赛
                continue
            try:
                idea_a = await session.get(Idea, uuid.UUID(str(pair[0])))
                idea_b = await session.get(Idea, uuid.UUID(str(pair[1])))
                if idea_a is None or idea_b is None:
                    raise ValueError("idea not found")
                result = await _run_match(ctx, session, idea_a, idea_b, match_no)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 — 单场辩论失败隔离
                failed.append(
                    {"pair": f"{pair[0]} vs {pair[1]}", "error": f"{type(e).__name__}: {e}"}
                )
                continue
            results.append(result)
            ctx.checkpoint["review_results"] = results

    ctx.checkpoint["review_results"] = results
    return {"matches": len(pairs), "completed": len(results), "failed": failed}


# ---- review 3. 汇总 ----


@register("review.summarize")
async def review_summarize(ctx: ActionContext, params: dict[str, Any]) -> dict[str, Any]:
    results: list[dict[str, Any]] = list(ctx.checkpoint.get("review_results") or [])
    async with get_sessionmaker()() as session:
        session.add(
            Activity(
                project_id=ctx.run.project_id,
                actor="agent:idea-review",
                kind="review.completed",
                message=f"Idea 评审锦标赛完成：{len(results)} 场辩论",
                payload={"voyage_id": str(ctx.run.id), "matches": len(results)},
            )
        )
        await session.commit()
    return {"matches": len(results), "results": results}
