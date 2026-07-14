"""paper_review voyage 动作（kind ``paper_review`` 固定管线执行体，docs/api-m5-c.md）。

流水线（navigator.paper_review_plan，固定六步）：
    review.citation_check → review.fact_check → review.render →
    review.referees（×3 + 逐员 guardrail）→ review.meta_review → review.guardrail

约定：
- 确定性逻辑（\\cite 解析、库内精确匹配、数字比对、\\ref/图检查、PDF 渲染、
  聚合公式）在 services/paper_review.py；只有判断性任务（支撑性、claim 抽查、
  评审员意见、guardrail 校验、meta summary）走 LLM（stage=review）；
- 评审员输出 JSON 严格校验重试 2；guardrail 未过重生成 ≤2 次，仍未过标
  ``unreliable`` 且不计入聚合；
- 结果落一个 ReviewSession(target_type="manuscript")，payload 按契约 §2 shape
  （citation_check / fact_check / meta / guardrail，附加 reviews 供前端结构化渲染）；
  逐 reviewer 意见与 meta 各发一条 ReviewMessage + WS ``review.message``；
- 通过判定（§4）：meta.rating ≥ 6 且无 fabricated → Manuscript.review_passed=true；
  不通过 → 修订说明写 fact_pack.revision_notes，under_review 稿件回 compiled
  并发 WS ``manuscript.status``；
- checkpoint 幂等：各步结果记入 checkpoint，断点续跑不重复调 LLM / 不重发消息。
"""

import asyncio
import json
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.voyage.actions import ActionContext, register
from app.core.db import get_sessionmaker
from app.core.llm.base import Message
from app.models.activity import Activity
from app.models.manuscript import Manuscript
from app.models.paper import Paper
from app.models.review import ReviewMessage, ReviewSession
from app.services import latex_compile
from app.services import paper_review as pr
from app.services.literature.openalex import OpenAlexClient
from app.services.literature.semantic_scholar import SemanticScholarClient
from app.services.review import serialize_message

_MAX_JSON_ATTEMPTS = 3  # 首次 + 重试 2 次（评审员 JSON 严格校验）
MAX_GUARDRAIL_REGENS = 2  # guardrail 未过重生成次数上限
MAX_REVIEW_PAGES = 9  # 编译 PDF 渲染前 9 页
RENDER_DPI = 120
MAX_IMAGE_BYTES = 4 * 1024 * 1024  # 单页 PNG 超 4MB 不送 LLM
MAX_SUPPORT_CHECKS = 30  # 支撑性 LLM 判定上限（超出标 not_checked）
_SOURCE_CHARS = 12_000  # LaTeX 源注入 prompt 的上限
_DIGEST_CHARS = 4_000  # 核验/查错摘要注入 prompt 的上限
META_AUTHOR = "主席 Meta"

# fake provider 识别标记（core/llm/fake.py 对齐）：POLARIS_REVIEW_SUPPORT /
# POLARIS_REVIEW_FACTCHECK / POLARIS_PAPER_REVIEWER / POLARIS_REVIEW_GUARDRAIL /
# POLARIS_REVIEW_META
SUPPORT_SYSTEM_PROMPT = """\
你是论文评审的引用核验员（POLARIS_REVIEW_SUPPORT），判断一处引用是否被被引论文支撑：
引用语境声称的内容，被引论文（标题/摘要/相关段落）是否真的支持。
只输出一个 JSON 对象，不要输出任何其他文字或 Markdown 代码块，格式：
{"support": "supported" 或 "partial" 或 "unsupported", "reason": "简要理由"}
"""

FACTCHECK_SYSTEM_PROMPT = """\
你是论文评审的事实核查员（POLARIS_REVIEW_FACTCHECK），对论文正文做 claim 抽查：
针对性提问式核对——正文的结论/对比/因果声明是否能从事实包（假设/指标/图表）推出，
夸大、无证据或与事实包矛盾的声明列为问题。
只输出一个 JSON 对象，不要输出任何其他文字或 Markdown 代码块，格式：
{"items": [{"location": "章节名或 file:line", "issue": "问题", "evidence": "依据",
"severity": "major" 或 "minor"}]}
没有问题输出 {"items": []}。
"""

REVIEWER_SYSTEM_PROMPT = """\
你是论文同行评审员（POLARIS_PAPER_REVIEWER），人设「{name}」（立场：{stance}）。
基于稿件渲染页、LaTeX 源与自动核验摘要给出严格评审。
只输出一个 JSON 对象，不要输出任何其他文字或 Markdown 代码块，格式：
{{"soundness": 1-4, "presentation": 1-4, "contribution": 1-4, "rating": 1-10,
"confidence": 1-5, "strengths": ["优点"], "weaknesses": ["不足"], "questions": ["问题"]}}
要求：意见必须引用论文实际内容、具体可操作，不得泛泛而谈或编造论文中不存在的内容。
"""

GUARDRAIL_SYSTEM_PROMPT = """\
你是评审质量守门员（POLARIS_REVIEW_GUARDRAIL），校验一份评审员意见能否发布：
是否引用了论文实际内容、是否具体（非模板化空话）、有无幻觉（提及论文中不存在的
方法/数字/图表）。
只输出一个 JSON 对象，不要输出任何其他文字，格式：
{"passed": true 或 false, "reason": "简要理由"}
"""

META_SYSTEM_PROMPT = """\
你是评审主席（POLARIS_REVIEW_META），综合各评审员意见与自动核验结果写 meta-review
总结：主要贡献、共识优点、关键不足与修订建议，中文 3-6 句。
只输出一个 JSON 对象，不要输出任何其他文字或 Markdown 代码块，格式：
{"summary": "总结文字"}
"""


# ---- 公共小件 ----


def _params(ctx: ActionContext) -> dict[str, Any]:
    params = (ctx.checkpoint or {}).get("params")
    return params if isinstance(params, dict) else {}


def _manuscript_id(ctx: ActionContext) -> uuid.UUID:
    raw = _params(ctx).get("manuscript_id")
    if not raw:
        raise ValueError("paper_review voyage 缺少 checkpoint.params.manuscript_id")
    return uuid.UUID(str(raw))


async def _get_manuscript(session: AsyncSession, ctx: ActionContext) -> Manuscript:
    manuscript = await session.get(Manuscript, _manuscript_id(ctx))
    if manuscript is None:
        raise ValueError(f"manuscript not found: {_manuscript_id(ctx)}")
    return manuscript


def _personas(ctx: ActionContext) -> list[dict[str, str]]:
    return pr.resolve_review_personas(_params(ctx).get("personas"))


def _fact_pack(manuscript: Manuscript) -> dict[str, Any]:
    return manuscript.fact_pack if isinstance(manuscript.fact_pack, dict) else {}


def _extract_json(content: str) -> Any:
    start = content.find("{")
    end = content.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object found")
    return json.loads(content[start : end + 1])


async def _complete_json(
    ctx: ActionContext,
    *,
    system: str,
    user: str,
    validate,
    images: list[bytes] | None = None,
) -> Any:
    """stage=review 的 JSON 请求：解析/校验失败重试（共 _MAX_JSON_ATTEMPTS 次）。"""
    last_error: Exception | None = None
    for _attempt in range(_MAX_JSON_ATTEMPTS):
        result = await ctx.llm.complete(
            "review",
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


async def _get_review_session(session: AsyncSession, ctx: ActionContext) -> ReviewSession:
    raw = ctx.checkpoint.get("review_session_id")
    if raw:
        found = await session.get(ReviewSession, uuid.UUID(str(raw)))
        if found is not None:
            return found
    review_session = ReviewSession(
        target_type="manuscript",
        target_id=_manuscript_id(ctx),
        payload={"manuscript_id": str(_manuscript_id(ctx)), "voyage_id": str(ctx.run.id)},
    )
    session.add(review_session)
    await session.commit()
    await session.refresh(review_session)
    ctx.checkpoint["review_session_id"] = str(review_session.id)
    return review_session


async def _merge_payload(
    session: AsyncSession, review_session: ReviewSession, updates: dict[str, Any]
) -> None:
    review_session.payload = dict(review_session.payload or {}) | updates
    await session.commit()


async def _publish_message(
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


# ---- 1. 引用核验 ----


def _validate_support(data: Any) -> dict[str, str]:
    if not isinstance(data, dict) or data.get("support") not in (
        "supported",
        "partial",
        "unsupported",
    ):
        raise ValueError('expected {"support": "supported|partial|unsupported"}')
    return {"support": str(data["support"]), "reason": str(data.get("reason") or "")}


async def _cited_paper_brief(session: AsyncSession, entry: dict[str, Any], context: str) -> str:
    """支撑性判定的被引论文材料：库内取摘要/TL;DR（有全文取相关段），否则条目元数据。"""
    raw_pid = entry.get("paper_id")
    paper = None
    if raw_pid:
        try:
            paper = await session.get(Paper, uuid.UUID(str(raw_pid)))
        except ValueError:
            paper = None
    if paper is None:
        return f"标题：{entry.get('title')}\n年份：{entry.get('year')}\n（非库内文献，仅有元数据）"
    parts = [f"标题：{paper.title}", f"摘要：{paper.abstract or '（无）'}"]
    if paper.tldr:
        parts.append(f"TL;DR：{paper.tldr}")
    if paper.full_text_path and Path(paper.full_text_path).is_file():
        full_text = Path(paper.full_text_path).read_text(encoding="utf-8", errors="replace")
        parts.append(f"全文相关段：{pr.relevant_excerpt(full_text, context)}")
    return "\n".join(parts)


@register("review.citation_check")
async def review_citation_check(ctx: ActionContext, params: dict[str, Any]) -> dict[str, Any]:
    if isinstance(ctx.checkpoint.get("citation_check"), dict):  # 断点幂等
        done = ctx.checkpoint["citation_check"]
        return {"total": done.get("total", 0), "skipped": True}

    s2 = SemanticScholarClient()
    openalex = OpenAlexClient()
    try:
        async with get_sessionmaker()() as session:
            manuscript = await _get_manuscript(session, ctx)
            review_session = await _get_review_session(session, ctx)
            files = await pr.load_tex_files(session, manuscript.id)
            fact_citations = list(_fact_pack(manuscript).get("citations") or [])
            cited = pr.extract_citations(files)
            items = await pr.check_citation_existence(
                session, cited, fact_citations, s2=s2, openalex=openalex
            )

            # 支撑性（LLM）：fabricated 不判；超出上限标 not_checked
            by_key = {str(c.get("bibkey")): c for c in fact_citations if c.get("bibkey")}
            checked = 0
            for item in items:
                if item["existence"] == "fabricated" or checked >= MAX_SUPPORT_CHECKS:
                    continue
                entry = by_key.get(item["bibkey"]) or {}
                brief = await _cited_paper_brief(session, entry, item["context_snippet"])
                user = (
                    f"引用语境（\\cite{{{item['bibkey']}}} 前后 2 句）：\n"
                    f"{item['context_snippet']}\n\n被引论文：\n{brief}"
                )
                try:
                    verdict = await _complete_json(
                        ctx, system=SUPPORT_SYSTEM_PROMPT, user=user, validate=_validate_support
                    )
                    item["support"] = verdict["support"]
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 — 单条支撑性判定失败不打断核验
                    item["support"] = "not_checked"
                checked += 1

            citation_check = {"total": len(items), "items": items}
            ctx.checkpoint["citation_check"] = citation_check
            await _merge_payload(session, review_session, {"citation_check": citation_check})
    finally:
        await s2.aclose()
        await openalex.aclose()

    counts: dict[str, int] = {}
    for item in citation_check["items"]:
        counts[item["existence"]] = counts.get(item["existence"], 0) + 1
    return {"total": citation_check["total"], "existence_counts": counts, "supported": checked}


# ---- 2. 事实查错 ----


def _validate_claim_items(data: Any) -> list[dict[str, str]]:
    raw = data.get("items") if isinstance(data, dict) else None
    if not isinstance(raw, list):
        raise ValueError('expected {"items": [...]}')
    items = []
    for item in raw:
        if not isinstance(item, dict) or not item.get("issue"):
            raise ValueError("claim item missing issue")
        severity = str(item.get("severity") or "minor")
        items.append(
            {
                "location": str(item.get("location") or "正文"),
                "issue": str(item["issue"]),
                "evidence": str(item.get("evidence") or ""),
                "kind": "unsupported_claim",
                "severity": severity if severity in ("major", "minor") else "minor",
            }
        )
    return items


@register("review.fact_check")
async def review_fact_check(ctx: ActionContext, params: dict[str, Any]) -> dict[str, Any]:
    if isinstance(ctx.checkpoint.get("fact_check"), dict):  # 断点幂等
        return {"items": len(ctx.checkpoint["fact_check"].get("items") or []), "skipped": True}

    async with get_sessionmaker()() as session:
        manuscript = await _get_manuscript(session, ctx)
        review_session = await _get_review_session(session, ctx)
        files = await pr.load_tex_files(session, manuscript.id)
        fact_pack = _fact_pack(manuscript)

    # 确定性：数字 ↔ metrics、\ref 悬空、图存在性
    items = pr.scan_fact_issues(files, fact_pack)
    deterministic = len(items)

    # claim 抽查（LLM 针对性提问式）；失败不阻塞（确定性结果仍落库）
    source = "\n\n".join(f"%% {path}\n{content}" for path, content in files)[:_SOURCE_CHARS]
    pack_brief = json.dumps(
        {k: fact_pack.get(k) for k in ("idea", "hypotheses", "metrics", "figures")},
        ensure_ascii=False,
    )[:_DIGEST_CHARS]
    claim_items: list[dict[str, str]] = []
    try:
        claim_items = await _complete_json(
            ctx,
            system=FACTCHECK_SYSTEM_PROMPT,
            user=f"事实包：\n{pack_brief}\n\n论文 LaTeX 源：\n{source}",
            validate=_validate_claim_items,
        )
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 — claim 抽查失败降级为仅确定性查错
        claim_items = []
    items.extend(claim_items)

    fact_check = {"items": items}
    ctx.checkpoint["fact_check"] = fact_check
    async with get_sessionmaker()() as session:
        review_session = await _get_review_session(session, ctx)
        await _merge_payload(session, review_session, {"fact_check": fact_check})
    return {"items": len(items), "deterministic": deterministic, "claims": len(claim_items)}


# ---- 3. 渲染稿件（pymupdf：编译 PDF 前 9 页 → PNG） ----


def _render_pdf_pages_sync(pdf_path: Path, out_dir: Path) -> list[str]:
    import pymupdf  # 延迟导入：仅在真正渲染时需要

    out_dir.mkdir(parents=True, exist_ok=True)
    paths: list[str] = []
    with pymupdf.open(pdf_path) as doc:
        for index, page in enumerate(doc):
            if index >= MAX_REVIEW_PAGES:
                break
            pix = page.get_pixmap(dpi=RENDER_DPI)
            target = out_dir / f"page_{index + 1}.png"
            target.write_bytes(pix.tobytes("png"))
            paths.append(str(target))
    return paths


@register("review.render")
async def review_render(ctx: ActionContext, params: dict[str, Any]) -> dict[str, Any]:
    if isinstance(ctx.checkpoint.get("review_pages"), list):  # 断点幂等
        return {"pages": len(ctx.checkpoint["review_pages"]), "skipped": True}

    async with get_sessionmaker()() as session:
        manuscript = await _get_manuscript(session, ctx)
        pdf = latex_compile.latest_ok_pdf(manuscript)
        version = int((manuscript.latest_compile or {}).get("version") or 0)

    pages: list[str] = []
    degraded: str | None = None
    if pdf is None:
        degraded = "无可用编译 PDF"
    else:
        out_dir = latex_compile.version_dir(_manuscript_id(ctx), version) / "review_pages"
        try:
            pages = await asyncio.to_thread(_render_pdf_pages_sync, pdf, out_dir)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 — 渲染失败降级为纯文本评审
            degraded = f"{type(e).__name__}: {e}"
    ctx.checkpoint["review_pages"] = pages
    return {"pages": len(pages), "degraded": degraded}


# ---- 4. 评审员 ×3（多模态 + 逐员 guardrail） ----


def _load_page_images(ctx: ActionContext) -> list[bytes]:
    images: list[bytes] = []
    for raw in ctx.checkpoint.get("review_pages") or []:
        path = Path(str(raw))
        if not path.is_file():
            continue
        data = path.read_bytes()
        if len(data) > MAX_IMAGE_BYTES:
            continue
        images.append(data)
    return images


def _check_digest(ctx: ActionContext) -> str:
    citation_check = ctx.checkpoint.get("citation_check") or {}
    fact_check = ctx.checkpoint.get("fact_check") or {}
    suspicious = [i for i in citation_check.get("items") or [] if i.get("existence") != "exact"]
    lines = [
        f"引用核验：共 {citation_check.get('total', 0)} 条，"
        f"可疑 {len(suspicious)} 条（minor/fabricated）",
    ]
    lines += [
        f"- \\cite{{{i.get('bibkey')}}}：existence={i.get('existence')} support={i.get('support')}"
        for i in suspicious[:10]
    ]
    issues = fact_check.get("items") or []
    lines.append(f"事实查错：{len(issues)} 条")
    lines += [f"- [{i.get('severity')}] {i.get('location')}：{i.get('issue')}" for i in issues[:10]]
    return "\n".join(lines)[:_DIGEST_CHARS]


async def _reviewer_user_prompt(ctx: ActionContext) -> str:
    async with get_sessionmaker()() as session:
        manuscript = await _get_manuscript(session, ctx)
        files = await pr.load_tex_files(session, manuscript.id)
    source = "\n\n".join(f"%% {path}\n{content}" for path, content in files)[:_SOURCE_CHARS]
    pages = len(ctx.checkpoint.get("review_pages") or [])
    return (
        f"论文标题：{manuscript.title}\n"
        f"（附稿件渲染页 {pages} 张）\n\n"
        f"自动核验/查错摘要：\n{_check_digest(ctx)}\n\n"
        f"LaTeX 源（节选）：\n{source}"
    )


async def _guardrail_check(ctx: ActionContext, persona_name: str, opinion_text: str) -> bool:
    def validate(data: Any) -> bool:
        if not isinstance(data, dict) or not isinstance(data.get("passed"), bool):
            raise ValueError('expected {"passed": bool}')
        return bool(data["passed"])

    user = f"评审员「{persona_name}」的意见：\n{opinion_text}"
    try:
        return await _complete_json(
            ctx, system=GUARDRAIL_SYSTEM_PROMPT, user=user, validate=validate
        )
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 — guardrail 自身持续非法 JSON：保守放行
        return True


def _opinion_markdown(review: dict[str, Any]) -> str:
    lines = [
        f"评分：soundness {review['soundness']:g}/4 · presentation "
        f"{review['presentation']:g}/4 · contribution {review['contribution']:g}/4 · "
        f"总评 {review['rating']:g}/10（信心 {review['confidence']:g}/5）",
    ]
    for title, field in (("优点", "strengths"), ("不足", "weaknesses"), ("问题", "questions")):
        entries = review.get(field) or []
        if entries:
            lines.append(f"\n### {title}")
            lines += [f"- {e}" for e in entries]
    if review.get("unreliable"):
        lines.insert(0, "⚠️ 该意见未通过 guardrail 校验（unreliable），不计入聚合。\n")
    return "\n".join(lines)


@register("review.referees")
async def review_referees(ctx: ActionContext, params: dict[str, Any]) -> dict[str, Any]:
    personas = _personas(ctx)
    reviews: list[dict[str, Any]] = list(ctx.checkpoint.get("reviews") or [])
    done_names = {r.get("persona") for r in reviews}
    images = _load_page_images(ctx)
    user_prompt = await _reviewer_user_prompt(ctx)
    failed: list[dict[str, str]] = []

    async with get_sessionmaker()() as session:
        review_session = await _get_review_session(session, ctx)
        for index, persona in enumerate(personas):
            if persona["name"] in done_names:  # 断点幂等：已完成评审员不重跑
                continue
            system = REVIEWER_SYSTEM_PROMPT.format(
                name=persona["name"], stance=persona.get("stance") or ""
            )
            review: dict[str, Any] | None = None
            unreliable = False
            regenerated = 0
            try:
                review = await _complete_json(
                    ctx,
                    system=system,
                    user=user_prompt,
                    validate=pr.validate_reviewer_json,
                    images=images or None,
                )
                # guardrail：未过重生成 ≤2 次，仍未过标 unreliable
                while not await _guardrail_check(ctx, persona["name"], _opinion_markdown(review)):
                    if regenerated >= MAX_GUARDRAIL_REGENS:
                        unreliable = True
                        break
                    regenerated += 1
                    review = await _complete_json(
                        ctx,
                        system=system,
                        user=(
                            user_prompt + "\n\n上一稿意见未通过质量校验（不够具体或含幻觉），"
                            "请重新给出更扎实、引用论文实际内容的评审意见。"
                        ),
                        validate=pr.validate_reviewer_json,
                        images=images or None,
                    )
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 — 单个评审员失败隔离（标 unreliable）
                failed.append({"persona": persona["name"], "error": f"{type(e).__name__}: {e}"})
                if review is None:
                    review = {
                        "soundness": 0.0,
                        "presentation": 0.0,
                        "contribution": 0.0,
                        "rating": 0.0,
                        "confidence": 0.0,
                        "strengths": [],
                        "weaknesses": [],
                        "questions": [],
                    }
                unreliable = True

            review = dict(review)
            review["persona"] = persona["name"]
            review["unreliable"] = unreliable
            review["regenerated"] = regenerated
            await _publish_message(
                session,
                ctx,
                review_session,
                author_name=persona["name"],
                content=_opinion_markdown(review),
                round_no=index + 1,
            )
            reviews.append(review)
            ctx.checkpoint["reviews"] = reviews

        await _merge_payload(session, review_session, {"reviews": reviews})

    return {
        "reviewers": len(reviews),
        "unreliable": sum(1 for r in reviews if r.get("unreliable")),
        "regenerated": sum(int(r.get("regenerated") or 0) for r in reviews),
        "with_images": len(images),
        "failed": failed,
    }


# ---- 5. 汇总（meta-review） ----


def _validate_summary(data: Any) -> str:
    if not isinstance(data, dict) or not str(data.get("summary") or "").strip():
        raise ValueError('expected {"summary": "..."}')
    return str(data["summary"]).strip()


@register("review.meta_review")
async def review_meta_review(ctx: ActionContext, params: dict[str, Any]) -> dict[str, Any]:
    if isinstance(ctx.checkpoint.get("meta"), dict):  # 断点幂等
        return {"rating": ctx.checkpoint["meta"].get("rating"), "skipped": True}

    reviews: list[dict[str, Any]] = list(ctx.checkpoint.get("reviews") or [])
    citation_check = ctx.checkpoint.get("citation_check") or {}
    has_fabricated = any(
        i.get("existence") == "fabricated" for i in citation_check.get("items") or []
    )
    reliable = [r for r in reviews if not r.get("unreliable")]

    aggregated = pr.aggregate_reviews(reviews)
    hint = pr.decision_hint(
        float(aggregated["rating"]), has_fabricated=has_fabricated, has_reliable=bool(reliable)
    )

    reviews_brief = json.dumps(
        [
            {k: r.get(k) for k in ("persona", "rating", "strengths", "weaknesses", "unreliable")}
            for r in reviews
        ],
        ensure_ascii=False,
    )[:_SOURCE_CHARS]
    try:
        summary = await _complete_json(
            ctx,
            system=META_SYSTEM_PROMPT,
            user=(
                f"聚合评分：{json.dumps(aggregated, ensure_ascii=False)}\n"
                f"自动核验摘要：\n{_check_digest(ctx)}\n"
                f"各评审员意见：\n{reviews_brief}"
            ),
            validate=_validate_summary,
        )
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 — meta LLM 失败降级为确定性总结
        summary = (
            f"共 {len(reviews)} 位评审员（可信 {len(reliable)} 位），"
            f"加权总评 {aggregated['rating']}/10，判定 {hint}。"
        )

    meta = {
        "soundness": aggregated["soundness"],
        "presentation": aggregated["presentation"],
        "contribution": aggregated["contribution"],
        "rating": aggregated["rating"],
        "decision_hint": hint,
        "summary": summary,
        "aggregation": aggregated["aggregation"],
    }
    ctx.checkpoint["meta"] = meta

    async with get_sessionmaker()() as session:
        review_session = await _get_review_session(session, ctx)
        await _publish_message(
            session,
            ctx,
            review_session,
            author_name=META_AUTHOR,
            content=(
                f"总评 {meta['rating']:g}/10（{hint}）· soundness {meta['soundness']:g} · "
                f"presentation {meta['presentation']:g} · contribution "
                f"{meta['contribution']:g}\n\n{summary}"
            ),
            round_no=len(reviews) + 1,
        )
        await _merge_payload(session, review_session, {"meta": meta})
    return {"rating": meta["rating"], "decision_hint": hint, "fabricated": has_fabricated}


# ---- 6. guardrail 校验（终判与流转） ----


@register("review.guardrail")
async def review_guardrail(ctx: ActionContext, params: dict[str, Any]) -> dict[str, Any]:
    if ctx.checkpoint.get("review_finalized"):  # 断点幂等
        return {"passed": ctx.checkpoint.get("review_result"), "skipped": True}

    reviews: list[dict[str, Any]] = list(ctx.checkpoint.get("reviews") or [])
    meta: dict[str, Any] = dict(ctx.checkpoint.get("meta") or {})
    citation_check: dict[str, Any] = dict(ctx.checkpoint.get("citation_check") or {})
    fact_check: dict[str, Any] = dict(ctx.checkpoint.get("fact_check") or {})

    unreliable_count = sum(1 for r in reviews if r.get("unreliable"))
    guardrail = {
        "passed": bool(reviews) and unreliable_count == 0,
        "regenerated": sum(int(r.get("regenerated") or 0) for r in reviews),
    }
    passed = pr.review_passed(meta, citation_check)

    async with get_sessionmaker()() as session:
        manuscript = await _get_manuscript(session, ctx)
        review_session = await _get_review_session(session, ctx)
        manuscript.review_passed = passed
        status_changed = False
        if not passed:
            # 修订说明写 fact_pack.revision_notes（下次 AI 起草/修订可引用）
            new_pack = dict(manuscript.fact_pack or {})
            new_pack["revision_notes"] = pr.build_revision_notes(
                reviews, fact_check.get("items") or [], citation_check.get("items") or []
            )
            manuscript.fact_pack = new_pack
            if manuscript.status == "under_review":  # 投稿审批中被否 → 回 compiled
                manuscript.status = "compiled"
                status_changed = True
        review_session.status = "closed"
        review_session.payload = dict(review_session.payload or {}) | {
            "guardrail": guardrail,
            "passed": passed,
        }
        session.add(
            Activity(
                project_id=manuscript.project_id,
                actor="agent:paper-review",
                kind="manuscript.review_completed",
                message=(
                    f"论文评审完成：{manuscript.title}"
                    f"（总评 {meta.get('rating', 0)}/10，{'通过' if passed else '未通过'}）"
                ),
                payload={
                    "manuscript_id": str(manuscript.id),
                    "session_id": str(review_session.id),
                    "rating": meta.get("rating"),
                    "review_passed": passed,
                },
            )
        )
        await session.commit()
        if status_changed:
            await ctx.notify(
                {
                    "type": "manuscript.status",
                    "manuscript_id": str(manuscript.id),
                    "status": manuscript.status,
                }
            )

    ctx.checkpoint["review_finalized"] = True
    ctx.checkpoint["review_result"] = passed
    return {
        "passed": passed,
        "guardrail": guardrail,
        "rating": meta.get("rating"),
        "unreliable": unreliable_count,
        "session_id": ctx.checkpoint.get("review_session_id"),
    }
