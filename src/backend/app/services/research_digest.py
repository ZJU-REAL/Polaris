"""文献库每日简报与滚动趋势的生成、持久化和读取。"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.llm.base import Message
from app.core.llm.router import LLMRouter
from app.models.library_direction import DirectionLibrary, LibraryPaper
from app.models.paper import Paper
from app.models.research_digest import LibraryResearchDigest
from app.models.voyage import VoyageRun
from app.services import concepts as concepts_service
from app.services.libraries import library_definition

logger = logging.getLogger(__name__)

PAPER_INSIGHTS_SYSTEM_PROMPT = """\
POLARIS_DAILY_DIGEST
POLARIS_DAILY_DIGEST_BATCH
你是科研文献库的论文解读编辑。依据给出的研究方向、相关性判断、论文 TL;DR、解读摘录和
已有概念，为本批每篇论文生成简洁、具体、可追溯的中文看点，并标注 2—4 个稳定、可复用的
学术概念。概念应使用规范、简短且跨论文一致的中文名称，优先复用本批已有概念；不要把论文名、
模型专名、作者名或完整句子当成概念，也不要输出 [[双链]]。不要补造输入中没有的实验数字或
结论，也不要遗漏或合并论文。
只输出一个 JSON 对象，不要输出 Markdown 代码块，格式：
{"paper_insights":[
  {"paper_id":"UUID", "highlight":"为什么值得看", "direction_relation":"与方向的具体关系",
   "concepts":["学术概念一","学术概念二"]}
]}
paper_insights 必须逐一覆盖输入里的全部 paper_id，数量必须与输入论文数量一致。
"""

DIGEST_SYNTHESIS_SYSTEM_PROMPT = """\
POLARIS_DAILY_DIGEST
POLARIS_DAILY_DIGEST_SYNTHESIS
你是科研文献库的每日简报主编。根据已经逐篇生成并校验完整的论文看点，综合本期总览与跨论文
共同信号。不要重新输出逐篇看点，不要补造输入中没有的实验数字或结论。
只输出一个 JSON 对象，不要输出 Markdown 代码块，格式：
{"summary":"本期总览", "cross_paper_signals":[
  {"title":"共同信号", "summary":"跨论文观察", "paper_ids":["UUID"]}
]}
没有可靠共同信号时 cross_paper_signals 返回空数组；信号中的 paper_ids 只能取输入已有 UUID。
"""

_DIGEST_BATCH_SIZE = 10
_DIGEST_MAX_ATTEMPTS = 3
_DIGEST_RETRY_DELAY_SECONDS = 0.25

TREND_SYSTEM_PROMPT = """\
POLARIS_TREND_SYNTHESIS
你维护一个研究方向的滚动趋势。根据此前趋势和最近每日简报，更新趋势线程；不要只追加日报，
要合并同一主线、说明证据如何变化，并保留支撑论文 id。只输出一个 JSON 对象，不要输出
Markdown 代码块，格式：
{"trends":[{"title":"趋势名", "status":"emerging|active|converging|stale",
"summary":"当前判断", "evidence_trajectory":"证据如何演进", "concepts":["概念"],
"paper_ids":["UUID"], "last_seen":"YYYY-MM-DD"}]}
最多 12 条；没有可靠趋势时返回 {"trends":[]}。
"""


def _extract_json_object(content: str) -> dict[str, Any]:
    start = content.find("{")
    end = content.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("digest model returned no JSON object")
    value = json.loads(content[start : end + 1])
    if not isinstance(value, dict):
        raise ValueError("digest model returned a non-object JSON value")
    return value


def _string(value: Any, fallback: str = "") -> str:
    text = str(value or "").strip()
    return text or fallback


def _generated_concepts(value: Any, existing: list[Any] | None = None) -> list[str]:
    """合并已有正式概念与模型标注，保序去重；最终可见性由概念服务统一裁决。"""
    values = [*(existing or []), *(value if isinstance(value, list) else [])]
    names: dict[str, None] = {}
    for raw in values:
        name = " ".join(str(raw or "").strip().split())
        if name.startswith("[[") and name.endswith("]]"):
            name = name[2:-2].strip()
        name = name.split("|", 1)[0].split("#", 1)[0].strip()
        if not name or len(name) > 255:
            continue
        names.setdefault(name, None)
        if len(names) >= 6:
            break
    return list(names)


async def _retry_pause(attempt: int) -> None:
    """简短指数退避；attempt 从 1 开始，测试和人工触发都不会产生长时间空等。"""
    await asyncio.sleep(_DIGEST_RETRY_DELAY_SECONDS * (2 ** (attempt - 1)))


async def _generate_insight_batch(
    *,
    llm: LLMRouter,
    run: VoyageRun,
    library: DirectionLibrary,
    direction: dict[str, Any],
    papers: list[dict[str, Any]],
    batch_index: int,
    batch_total: int,
    preferred_concepts: list[str],
    user_id: uuid.UUID | None,
    extra_guidance: str,
) -> tuple[dict[str, dict[str, Any]], str | None, int, str | None]:
    """生成一批逐篇看点；模型漏项时只把缺失论文送回去补跑，最多三轮。

    返回 (已生成的洞察, 模型名, 重试次数, 失败原因或 None)。三轮之后仍有缺失也
    **不抛错**：把已经拿到的那部分交回去，缺的记进原因。摘要是附加产物，不该因为
    模型偶尔漏几篇就把整轮同步打成 paused_error——前面的候选、打分、取全文、编译、
    概念上链都已经落库了。"""
    expected = {item["paper_id"]: item for item in papers}
    pending = list(papers)
    collected: dict[str, dict[str, Any]] = {}
    model: str | None = None
    retries = 0
    last_error: Exception | None = None

    for attempt in range(1, _DIGEST_MAX_ATTEMPTS + 1):
        retry_note = (
            "\n这是自动补跑，只需覆盖以下仍缺失的论文；必须逐一返回全部 paper_id。"
            if attempt > 1
            else ""
        )
        prompt = (
            f"研究方向：{json.dumps(direction, ensure_ascii=False)}\n"
            f"优先复用的规范概念词表：{json.dumps(preferred_concepts, ensure_ascii=False)}\n"
            f"批次：{batch_index}/{batch_total}\n"
            f"本批论文：{json.dumps(pending, ensure_ascii=False)}"
            f"{retry_note}"
        )
        try:
            result = await llm.complete(
                "digest",
                [
                    Message(
                        role="system",
                        content=PAPER_INSIGHTS_SYSTEM_PROMPT + extra_guidance,
                    ),
                    Message(role="user", content=prompt),
                ],
                user_id=user_id,
                project_id=run.project_id,
                library_id=library.id,
                voyage_id=run.id,
            )
            generated = _extract_json_object(result.content)
            raw_insights = generated.get("paper_insights")
            if not isinstance(raw_insights, list):
                raise ValueError("daily digest batch JSON misses paper_insights")
            model = result.model or model
            for item in raw_insights:
                if not isinstance(item, dict):
                    continue
                paper_id = str(item.get("paper_id") or "")
                concepts = _generated_concepts(
                    item.get("concepts"), expected.get(paper_id, {}).get("concepts")
                )
                # 概念是简报必填项；缺失时把该论文留在 pending，下一轮只补跑这一篇。
                if paper_id in expected and concepts:
                    collected[paper_id] = item
            pending = [item for item in papers if item["paper_id"] not in collected]
            if not pending:
                return collected, model, retries, None
            last_error = ValueError(
                f"daily digest batch {batch_index}/{batch_total} omitted {len(pending)} papers"
            )
        except Exception as error:
            last_error = error

        if attempt < _DIGEST_MAX_ATTEMPTS:
            retries += 1
            await _retry_pause(attempt)

    # 这一批没能生成洞察：记下原因交回给调用方，**不抛错**。
    # 以前是直接 raise，于是整轮同步被打成 paused_error——前面五步（候选、打分、
    # 取全文、编译、概念上链）的成果全被最后一步的摘要生成拖死，而摘要本身只是
    # 附加产物。模型偶尔不把一批论文的 id 全数返回是常态，不该升级成致命错误。
    missing_ids = [item["paper_id"] for item in pending]
    detail = f"; missing={','.join(missing_ids)}" if missing_ids else ""
    reason = (
        f"batch {batch_index}/{batch_total} failed after "
        f"{_DIGEST_MAX_ATTEMPTS} attempts: {last_error}{detail}"
    )
    logger.warning("daily digest %s", reason)
    # 交回**已经生成的**部分，而不是整批丢掉：前几轮拿到的洞察是有效的
    return collected, model, retries, reason


async def _generate_paper_insights(
    *,
    llm: LLMRouter,
    run: VoyageRun,
    library: DirectionLibrary,
    papers: list[dict[str, Any]],
    user_id: uuid.UUID | None,
    extra_guidance: str,
) -> tuple[list[dict[str, Any]], str | None, int, int, list[str]]:
    """按固定小批次生成看点，并保持最终顺序与输入论文一致。"""
    direction = library_definition(library)
    batches = [
        papers[index : index + _DIGEST_BATCH_SIZE]
        for index in range(0, len(papers), _DIGEST_BATCH_SIZE)
    ]
    by_id: dict[str, dict[str, Any]] = {}
    preferred_concepts = _generated_concepts(
        [], [concept for paper in papers for concept in (paper.get("concepts") or [])]
    )
    model: str | None = None
    retries = 0
    failed_batches: list[str] = []
    for index, batch in enumerate(batches, start=1):
        generated, batch_model, batch_retries, failure = await _generate_insight_batch(
            llm=llm,
            run=run,
            library=library,
            direction=direction,
            papers=batch,
            batch_index=index,
            batch_total=len(batches),
            preferred_concepts=preferred_concepts,
            user_id=user_id,
            extra_guidance=extra_guidance,
        )
        by_id.update(generated)
        preferred_concepts = _generated_concepts(
            [concept for item in generated.values() for concept in (item.get("concepts") or [])],
            preferred_concepts,
        )
        model = batch_model or model
        retries += batch_retries
        if failure:
            failed_batches.append(failure)

    # 批次彻底失败时 by_id 不再覆盖全部论文：缺项的论文照样列出来，只是看点退回它自己的
    # TL;DR。少一句模型措辞，好过让这篇论文从简报里凭空消失。
    insights = [
        item
        | {
            "highlight": _string(by_id.get(item["paper_id"], {}).get("highlight"), item["tldr"]),
            "direction_relation": _string(
                by_id.get(item["paper_id"], {}).get("direction_relation"),
                item.get("relevance_reason") or "与本库研究方向相关。",
            ),
            "concepts": _generated_concepts(
                by_id.get(item["paper_id"], {}).get("concepts"), item.get("concepts")
            ),
        }
        for item in papers
    ]
    return insights, model, retries, len(batches), failed_batches


async def _synthesize_digest_summary(
    *,
    llm: LLMRouter,
    run: VoyageRun,
    library: DirectionLibrary,
    paper_insights: list[dict[str, Any]],
    user_id: uuid.UUID | None,
    extra_guidance: str,
) -> tuple[str, list[dict[str, Any]], str | None, int]:
    """在逐篇结果完整后综合总览/共同信号；格式错误或临时异常自动重试。"""
    compact_insights = [
        {
            "paper_id": item["paper_id"],
            "title": item["title"],
            "tldr": _string(item.get("tldr"))[:400],
            "highlight": item["highlight"],
            "direction_relation": item["direction_relation"],
            "concepts": item.get("concepts") or [],
        }
        for item in paper_insights
    ]
    valid_ids = {item["paper_id"] for item in paper_insights}
    last_error: Exception | None = None
    for attempt in range(1, _DIGEST_MAX_ATTEMPTS + 1):
        prompt = (
            "研究方向："
            + json.dumps(library_definition(library), ensure_ascii=False)
            + "\n已校验完整的逐篇看点："
            + json.dumps(compact_insights, ensure_ascii=False)
        )
        if attempt > 1:
            prompt += "\n上次综合输出无效，请严格按 JSON schema 重新生成。"
        try:
            result = await llm.complete(
                "digest",
                [
                    Message(
                        role="system",
                        content=DIGEST_SYNTHESIS_SYSTEM_PROMPT + extra_guidance,
                    ),
                    Message(role="user", content=prompt),
                ],
                user_id=user_id,
                project_id=run.project_id,
                library_id=library.id,
                voyage_id=run.id,
            )
            generated = _extract_json_object(result.content)
            if not isinstance(generated.get("cross_paper_signals"), list):
                raise ValueError("daily digest synthesis JSON misses cross_paper_signals")
            summary = _string(generated.get("summary"))
            if not summary:
                raise ValueError("daily digest synthesis JSON misses summary")
            signals = [
                {
                    "title": _string(item.get("title"), "共同信号"),
                    "summary": _string(item.get("summary")),
                    "paper_ids": [
                        str(paper_id)
                        for paper_id in (item.get("paper_ids") or [])
                        if str(paper_id) in valid_ids
                    ],
                }
                for item in generated["cross_paper_signals"]
                if isinstance(item, dict) and _string(item.get("summary"))
            ]
            return summary, signals, result.model or None, attempt - 1
        except Exception as error:
            last_error = error
            if attempt < _DIGEST_MAX_ATTEMPTS:
                await _retry_pause(attempt)

    raise ValueError(
        f"daily digest synthesis failed after {_DIGEST_MAX_ATTEMPTS} attempts: {last_error}"
    )


def _search_counts(checkpoint: dict[str, Any]) -> tuple[dict[str, int], dict[str, Any]]:
    stats = checkpoint.get("ingest_search_stats")
    diagnostics = dict(stats) if isinstance(stats, dict) else {}
    override = checkpoint.get("digest_counts")
    if isinstance(override, dict):
        return (
            {
                key: int(override.get(key) or 0)
                for key in (
                    "source_fetched",
                    "prescreened",
                    "inserted",
                    "kept",
                    "excluded",
                    "compiled",
                )
            },
            diagnostics,
        )
    query_found = int(diagnostics.get("query_found") or diagnostics.get("found") or 0)
    rss_found = int(diagnostics.get("rss_found") or 0)
    rss_matched = int(diagnostics.get("rss_matched") or 0)
    source_fetched = int(diagnostics.get("source_fetched") or query_found + rss_found)
    prescreened = int(diagnostics.get("prescreened") or query_found + rss_matched)
    counts = {
        "source_fetched": source_fetched,
        # API 主查询已经服务端关键词筛过；RSS 是分类全量后本地预筛。
        "prescreened": prescreened,
        "inserted": int(diagnostics.get("inserted") or 0)
        + int(checkpoint.get("ingest_snowball_inserted") or 0),
        "kept": 0,
        "excluded": 0,
        "compiled": int(checkpoint.get("compiled_count") or 0),
    }
    return counts, diagnostics


async def _run_members(
    session: AsyncSession,
    *,
    library_id: uuid.UUID,
    run: VoyageRun,
    checkpoint: dict[str, Any],
) -> list[tuple[Paper, LibraryPaper]]:
    """取本次 run 实际完成过相关性评分的成员；逐篇 commit/断点恢复也不会漏。"""
    raw_ids = checkpoint.get("digest_paper_ids")
    paper_ids: list[uuid.UUID] = []
    if isinstance(raw_ids, list):
        for value in raw_ids:
            try:
                paper_ids.append(uuid.UUID(str(value)))
            except (TypeError, ValueError):
                continue
    member_window = (
        (LibraryPaper.paper_id.in_(paper_ids),)
        if paper_ids
        else (
            LibraryPaper.scored_at.is_not(None),
            LibraryPaper.scored_at >= run.created_at,
        )
    )
    return list(
        (
            await session.execute(
                select(Paper, LibraryPaper)
                .join(LibraryPaper, LibraryPaper.paper_id == Paper.id)
                .options(selectinload(Paper.concepts), selectinload(Paper.wiki))
                .where(
                    LibraryPaper.library_id == library_id,
                    *member_window,
                )
                .order_by(LibraryPaper.relevance_score.desc().nulls_last(), Paper.title)
            )
        ).all()
    )


def _paper_payload(paper: Paper, membership: LibraryPaper) -> dict[str, Any]:
    concepts = [concept.name for concept in paper.concepts]
    wiki_excerpt = (paper.wiki_content or "").strip().replace("\x00", "")[:900]
    return {
        "paper_id": str(paper.id),
        "title": paper.title,
        "published_at": paper.published_at.isoformat() if paper.published_at else None,
        "year": paper.year,
        "relevance_score": membership.relevance_score,
        "relevance_reason": membership.relevance_reason,
        "tldr": paper.tldr or membership.tldr_note or paper.abstract or "暂无摘要",
        "concepts": concepts,
        "wiki_excerpt": wiki_excerpt,
        "status": membership.status,
    }


def _render_digest_markdown(
    *,
    report_date: date,
    summary: str,
    counts: dict[str, int],
    diagnostics: dict[str, Any],
    papers: list[dict[str, Any]],
    signals: list[dict[str, Any]],
    excluded: list[dict[str, Any]],
) -> str:
    lines = [
        f"# 每日文献简报 · {report_date.isoformat()}",
        "",
        summary,
        "",
        "## 本次概况",
        "",
        f"- 来源抓取：{counts['source_fetched']} 篇；预筛后：{counts['prescreened']} 篇",
        f"- 新候选：{counts['inserted']} 篇；保留：{counts['kept']} 篇；"
        f"排除：{counts['excluded']} 篇",
        f"- 完成解读：{counts['compiled']} 篇",
    ]
    latest = diagnostics.get("source_latest_at")
    if latest:
        lines.append(f"- 数据源最新公告：{latest}")
    generation = diagnostics.get("digest_generation")
    if isinstance(generation, dict):
        lines.append(
            f"- 简报生成：{int(generation.get('batches') or 0)} 批；"
            f"自动重试：{int(generation.get('retries') or 0)} 次"
        )
        lines.append(
            f"- 概念关联：{int(generation.get('active_concepts') or 0)} 个正式概念；"
            f"新增 {int(generation.get('links_created') or 0)} 条论文关系"
        )
        failed = generation.get("failed_batches")
        if isinstance(failed, list) and failed:
            # 降级说明必须写在正文里：这几批的论文只有 TL;DR，没有模型写的看点，
            # 否则读者会以为看点写得敷衍，而不知道是模型没出结果。
            lines.append(
                f"- ⚠️ {len(failed)} 批未能生成看点，相关论文暂以 TL;DR 代替："
                + "；".join(str(item) for item in failed)
            )
    messages = diagnostics.get("messages") if isinstance(diagnostics.get("messages"), list) else []
    if messages:
        lines.extend(["", "### 来源诊断", ""] + [f"- {message}" for message in messages])

    lines.extend(["", "## 本期论文", ""])
    if not papers:
        lines.append("本次没有通过相关性阈值的新论文。")
    for item in papers:
        score = item.get("relevance_score")
        score_label = f" · 相关度 {float(score):.2f}" if score is not None else ""
        published_label = _string(item.get("published_at"))[:10]
        if not published_label and item.get("year"):
            published_label = str(item["year"])
        published_label = published_label or "未知"
        lines.extend(
            [
                f"### [[paper:{item['paper_id']}|{item['title']}]]{score_label}",
                "",
                f"- **发表时间**：{published_label}",
                f"- **TL;DR**：{item['tldr']}",
                f"- **看点**：{item['highlight']}",
                f"- **与方向关系**：{item['direction_relation']}",
            ]
        )
        concepts = item.get("concepts") or []
        if concepts:
            lines.append("- **概念**：" + " · ".join(f"[[{name}]]" for name in concepts))
        else:
            lines.append("- **概念**：尚无达到复用门槛的正式概念")
        lines.append("")

    lines.extend(["## 跨论文观察", ""])
    if signals:
        for index, signal in enumerate(signals, start=1):
            lines.append(f"{index}. **{signal['title']}**：{signal['summary']}")
    else:
        lines.append("本次尚未形成可靠的跨论文共同信号。")

    lines.extend(["", f"## 排除清单（{len(excluded)} 篇）", ""])
    if excluded:
        for item in excluded:
            score = item.get("relevance_score")
            score_label = f"{float(score):.2f}" if score is not None else "未评分"
            lines.append(f"- `{score_label}` · {item['title']} — {item['reason']}")
    else:
        lines.append("本次没有因相关性不足而排除的论文。")
    return "\n".join(lines).strip() + "\n"


async def create_daily_digest(
    session: AsyncSession,
    *,
    run: VoyageRun,
    library: DirectionLibrary,
    checkpoint: dict[str, Any],
    llm: LLMRouter,
    user_id: uuid.UUID | None,
    extra_guidance: str = "",
) -> LibraryResearchDigest:
    """生成并持久化本次简报；模型产出无效会抛错，让 Voyage 停在水位线之前。"""
    existing = (
        await session.execute(
            select(LibraryResearchDigest).where(LibraryResearchDigest.voyage_id == run.id)
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    pairs = await _run_members(session, library_id=library.id, run=run, checkpoint=checkpoint)
    kept_pairs = [(p, m) for p, m in pairs if m.status != "excluded"]
    excluded_pairs = [(p, m) for p, m in pairs if m.status == "excluded"]
    counts, diagnostics = _search_counts(checkpoint)
    counts["kept"] = len(kept_pairs)
    checkpoint_excluded = [
        item
        for item in (checkpoint.get("digest_excluded_papers") or [])
        if isinstance(item, dict) and item.get("paper_id")
    ]
    counts["excluded"] = len(excluded_pairs) + len(checkpoint_excluded)

    base_papers = [_paper_payload(paper, membership) for paper, membership in kept_pairs]
    excluded = [
        {
            "paper_id": str(paper.id),
            "title": paper.title,
            "relevance_score": membership.relevance_score,
            "reason": membership.relevance_reason
            or f"相关度低于本次阈值（{membership.relevance_score or 0:.2f}）",
        }
        for paper, membership in excluded_pairs
    ] + checkpoint_excluded

    model: str | None = None
    if base_papers:
        (
            paper_insights,
            insight_model,
            insight_retries,
            batch_count,
            insight_failures,
        ) = await _generate_paper_insights(
            llm=llm,
            run=run,
            library=library,
            papers=base_papers,
            user_id=user_id,
            extra_guidance=extra_guidance,
        )
        active_concepts, concept_stats = await concepts_service.link_generated_paper_concepts(
            session,
            concepts_by_paper={
                item["paper_id"]: item.get("concepts") or [] for item in paper_insights
            },
            llm=llm,
            user_id=user_id,
            project_id=run.project_id,
            library_id=library.id,
            voyage_id=run.id,
        )
        paper_insights = [
            item | {"concepts": active_concepts.get(item["paper_id"], [])}
            for item in paper_insights
        ]
        summary, signals, synthesis_model, synthesis_retries = await _synthesize_digest_summary(
            llm=llm,
            run=run,
            library=library,
            paper_insights=paper_insights,
            user_id=user_id,
            extra_guidance=extra_guidance,
        )
        diagnostics["digest_generation"] = {
            "batch_size": _DIGEST_BATCH_SIZE,
            "batches": batch_count,
            "retries": insight_retries + synthesis_retries,
            # 哪几批没能生成洞察（以前这里是直接抛错、整轮同步被打成 paused_error）
            "failed_batches": insight_failures,
            **concept_stats,
        }
        model = synthesis_model or insight_model
    else:
        paper_insights = []
        signals = []
        summary = "本次没有通过相关性阈值的新论文；已保留来源诊断供复查。"

    report_date = run.created_at.date()
    content = _render_digest_markdown(
        report_date=report_date,
        summary=summary,
        counts=counts,
        diagnostics=diagnostics,
        papers=paper_insights,
        signals=signals,
        excluded=excluded,
    )
    # 同一天重复手动同步时更新当天原生简报，而不是制造多个“每日”条目。
    digest = (
        await session.execute(
            select(LibraryResearchDigest).where(
                LibraryResearchDigest.library_id == library.id,
                LibraryResearchDigest.report_date == report_date,
                LibraryResearchDigest.source == "voyage",
            )
        )
    ).scalar_one_or_none()
    if digest is None:
        digest = LibraryResearchDigest(
            library_id=library.id,
            voyage_id=run.id,
            report_date=report_date,
            source="voyage",
            mode="bootstrap" if run.kind == "wiki_bootstrap" else "incremental",
            counts=counts,
            source_diagnostics=diagnostics,
            paper_insights=paper_insights,
            excluded_papers=excluded,
            cross_paper_signals=signals,
            summary=summary,
            content=content,
            model=model,
            rolling_trends=[],
            trend_content="",
            trend_model=None,
        )
        session.add(digest)
    else:
        digest.voyage_id = run.id
        digest.mode = "bootstrap" if run.kind == "wiki_bootstrap" else "incremental"
        digest.counts = counts
        digest.source_diagnostics = diagnostics
        digest.paper_insights = paper_insights
        digest.excluded_papers = excluded
        digest.cross_paper_signals = signals
        digest.summary = summary
        digest.content = content
        digest.model = model
        # 新一次同步必须重新综合趋势；不能把旧快照误当成本次已完成。
        digest.rolling_trends = []
        digest.trend_content = ""
        digest.trend_model = None
    await session.commit()
    await session.refresh(digest)
    return digest


def _render_trends_markdown(trends: list[dict[str, Any]], report_date: date) -> str:
    lines = [f"# 滚动趋势 · 更新至 {report_date.isoformat()}", ""]
    if not trends:
        lines.append("目前尚未形成有足够证据支撑的趋势主线。")
        return "\n".join(lines) + "\n"
    status_labels = {
        "emerging": "新兴",
        "active": "活跃",
        "converging": "收敛中",
        "stale": "暂时沉寂",
    }
    for trend in trends:
        status = status_labels.get(str(trend.get("status")), str(trend.get("status") or "活跃"))
        lines.extend(
            [
                f"## {trend['title']} · {status}",
                "",
                trend["summary"],
                "",
                f"**证据轨迹**：{trend['evidence_trajectory']}",
            ]
        )
        concepts = trend.get("concepts") or []
        if concepts:
            lines.append("\n**相关概念**：" + " · ".join(f"[[{name}]]" for name in concepts))
        lines.append("")
    return "\n".join(lines).strip() + "\n"


async def synthesize_rolling_trends(
    session: AsyncSession,
    *,
    run: VoyageRun,
    library: DirectionLibrary,
    llm: LLMRouter,
    user_id: uuid.UUID | None,
    extra_guidance: str = "",
) -> LibraryResearchDigest:
    digest = (
        await session.execute(
            select(LibraryResearchDigest).where(LibraryResearchDigest.voyage_id == run.id)
        )
    ).scalar_one_or_none()
    if digest is None:
        raise ValueError("daily digest is missing; refusing to synthesize trends")
    if digest.trend_model is not None:
        return digest

    previous = list(
        (
            await session.execute(
                select(LibraryResearchDigest)
                .where(
                    LibraryResearchDigest.library_id == library.id,
                    LibraryResearchDigest.id != digest.id,
                )
                .order_by(
                    LibraryResearchDigest.report_date.desc(),
                    LibraryResearchDigest.created_at.desc(),
                )
                .limit(8)
            )
        )
        .scalars()
        .all()
    )
    if not digest.paper_insights:
        prior = previous[0] if previous else None
        digest.rolling_trends = list(prior.rolling_trends or []) if prior else []
        digest.trend_content = (
            prior.trend_content
            if prior and prior.trend_content
            else _render_trends_markdown([], digest.report_date)
        )
        digest.trend_model = "carried-forward"
        await session.commit()
        return digest

    history = [
        {
            "date": item.report_date.isoformat(),
            "summary": item.summary,
            "signals": item.cross_paper_signals,
        }
        for item in reversed(previous)
    ]
    payload = {
        "research_direction": library_definition(library),
        "previous_trends": previous[0].rolling_trends if previous else [],
        "recent_reports": history
        + [
            {
                "date": digest.report_date.isoformat(),
                "summary": digest.summary,
                "signals": digest.cross_paper_signals,
                "papers": digest.paper_insights,
            }
        ],
    }
    result = await llm.complete(
        "digest",
        [
            Message(role="system", content=TREND_SYSTEM_PROMPT + extra_guidance),
            Message(role="user", content=json.dumps(payload, ensure_ascii=False)),
        ],
        user_id=user_id,
        project_id=run.project_id,
        library_id=library.id,
        voyage_id=run.id,
    )
    generated = _extract_json_object(result.content)
    if not isinstance(generated.get("trends"), list):
        raise ValueError("trend synthesis JSON misses trends")
    allowed_status = {"emerging", "active", "converging", "stale"}
    trends: list[dict[str, Any]] = []
    for item in generated["trends"][:12]:
        if not isinstance(item, dict):
            continue
        title = _string(item.get("title"))
        summary = _string(item.get("summary"))
        trajectory = _string(item.get("evidence_trajectory"))
        if not (title and summary and trajectory):
            continue
        status = str(item.get("status") or "active")
        trends.append(
            {
                "title": title,
                "status": status if status in allowed_status else "active",
                "summary": summary,
                "evidence_trajectory": trajectory,
                "concepts": [str(name) for name in (item.get("concepts") or [])],
                "paper_ids": [str(pid) for pid in (item.get("paper_ids") or [])],
                "last_seen": _string(item.get("last_seen"), digest.report_date.isoformat()),
            }
        )
    digest.rolling_trends = trends
    digest.trend_content = _render_trends_markdown(trends, digest.report_date)
    digest.trend_model = result.model or "unknown"
    await session.commit()
    return digest


async def list_digest_summaries(
    session: AsyncSession, *, library_id: uuid.UUID, limit: int
) -> list[dict[str, Any]]:
    rows = list(
        (
            await session.execute(
                select(LibraryResearchDigest)
                .where(LibraryResearchDigest.library_id == library_id)
                .order_by(
                    LibraryResearchDigest.report_date.desc(),
                    LibraryResearchDigest.created_at.desc(),
                )
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return [
        {
            "id": row.id,
            "report_date": row.report_date,
            "source": row.source,
            "mode": row.mode,
            "counts": row.counts,
            "summary": row.summary,
            "voyage_id": row.voyage_id,
            "has_trends": bool(row.trend_content or row.rolling_trends),
            "created_at": row.created_at,
        }
        for row in rows
    ]


async def get_digest(
    session: AsyncSession, *, library_id: uuid.UUID, digest_id: uuid.UUID
) -> LibraryResearchDigest | None:
    return (
        await session.execute(
            select(LibraryResearchDigest).where(
                LibraryResearchDigest.id == digest_id,
                LibraryResearchDigest.library_id == library_id,
            )
        )
    ).scalar_one_or_none()
