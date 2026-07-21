"""wiki ingest 动作（Voyage kinds ``wiki_bootstrap`` / ``wiki_ingest`` 的固定计划执行体）。

流水线（docs/api-m2.md §7）：
    wiki.search_candidates → wiki.snowball → wiki.score_relevance →
    wiki.fetch_extract → wiki.compile → wiki.link_concepts → wiki.update_watermark

健壮性约定：
- 每篇论文独立 try/except，单篇失败不打断批处理；observation 汇总
  {processed, succeeded, failed: [{id, error}]}；
- 断点恢复按 Paper.status 幂等：已打分/已编译的论文不会重复调 LLM；
- 判断性任务（打分/编译/概念定义）走 core/llm 路由，其余全为确定性代码。
"""

import asyncio
import logging
import re
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.voyage.actions import ActionContext, register
from app.core.db import get_sessionmaker
from app.models.activity import Activity
from app.models.base import utcnow
from app.models.paper import Paper
from app.models.project import Project
from app.services.chunks import embed_pending_chunks, index_paper_fulltext
from app.services.concepts import link_all_paper_concepts
from app.services.figure_annotate import annotate_figures, figures_annotated
from app.services.literature import get_arxiv_client, get_openalex_client, get_s2_client
from app.services.literature.arxiv import normalize_arxiv_id
from app.services.literature.pdf_extract import extract_figures, extract_full_text, save_pdf
from app.services.projects import DEFAULT_ARXIV_CATEGORIES
from app.services.relevance import build_relevance_context, score_paper_relevance
from app.services.wiki_compile import compile_paper

logger = logging.getLogger(__name__)

DEFAULT_KNOBS: dict[str, Any] = {
    "months_back": 6,
    "max_papers": 50,
    "relevance_threshold": 0.6,
    "snowball_depth": 1,
    "compile_top_n": 20,
    "unlimited": False,
}

_MAX_CANDIDATES_CAP = 200

# 最大化模式（knobs.unlimited=True）的安全哨兵：检索/打分/抽取/编译均按「窗口内全量」
# 处理，不再受 max_papers/compile_top_n/_MAX_CANDIDATES_CAP 截断；此哨兵只防真正失控
# （查询发散/外部 API 异常返回海量条目），正常调研窗口远达不到。
_UNLIMITED_SENTINEL = 10_000

# 最大化模式下单次运行补齐 chunk embedding 的哨兵上限（全量论文 × 每篇 ≤120 段）；
# 默认模式沿用 services/chunks.py 的 2000/次（剩余随每日同步补齐）。
_UNLIMITED_CHUNK_SENTINEL = 1_000_000


def _unlimited(knobs: dict[str, Any]) -> bool:
    return bool(knobs.get("unlimited"))


# 增量同步回看窗口：arXiv 关键词检索索引对新论文有约 3-5 天滞后（近 2 天窗口常搜到 0），
# 只回看 1 天会永远漏掉「延迟才被索引」的论文。放宽到 14 天，去重（arxiv_id/doi/title）
# 会跳过已入库的，故重叠扫描几乎零成本。
_INCREMENTAL_LOOKBACK_DAYS = 14

# observation 里给用户看的论文/概念清单上限（避免 observation JSON 过大）
_OBS_LIST_CAP = 30

# 打分/编译的逐篇 LLM 调用有界并发上限：底层走 LiteLLM（有速率限制），保守取 5。
# 每个并发任务用自己独立的 AsyncSession（见 _gather_bounded 调用点），逐篇 commit，
# 断点续跑语义（按 Paper.status 幂等）保持不变。
_LLM_CONCURRENCY = 5


async def _gather_bounded(limit: int, coros: list[Any]) -> list[Any]:
    """信号量限流地并发跑一批协程，返回结果列表（异常以对象形式就地保留）。

    - 单篇失败被 ``return_exceptions=True`` 捕获为结果项，不打断其他并发任务；
    - CancelledError（worker 被杀）也会被捕获为结果项——调用方需在汇总前检测并原样
      上抛，才能触发引擎的断点续跑（否则会被误当作单篇失败吞掉）。
    """
    sem = asyncio.Semaphore(limit)

    async def run(coro: Any) -> Any:
        async with sem:
            return await coro

    return await asyncio.gather(*(run(c) for c in coros), return_exceptions=True)


def _reraise_if_cancelled(results: list[Any]) -> None:
    """并发结果里若含 CancelledError（worker 被杀），原样上抛以触发断点续跑。"""
    for result in results:
        if isinstance(result, asyncio.CancelledError):
            raise result


def _paper_brief(papers: list[Paper]) -> list[dict[str, str]]:
    return [{"id": str(p.id), "title": p.title} for p in papers[:_OBS_LIST_CAP]]


# ---- 公共小件 ----


def resolve_knobs(raw: Any) -> dict[str, Any]:
    knobs = dict(DEFAULT_KNOBS)
    if isinstance(raw, dict):
        for key in DEFAULT_KNOBS:
            if raw.get(key) is not None:
                knobs[key] = raw[key]
    return knobs


def _params(ctx: ActionContext) -> dict[str, Any]:
    params = (ctx.checkpoint or {}).get("params")
    return params if isinstance(params, dict) else {}


def _knobs(ctx: ActionContext) -> dict[str, Any]:
    return resolve_knobs(_params(ctx).get("knobs"))


def _mode(ctx: ActionContext) -> str:
    mode = _params(ctx).get("mode")
    return (
        mode
        if mode in ("bootstrap", "incremental")
        else ("bootstrap" if ctx.run.kind == "wiki_bootstrap" else "incremental")
    )


async def _get_project(session: AsyncSession, ctx: ActionContext) -> Project:
    project = await session.get(Project, ctx.run.project_id)
    if project is None:
        raise ValueError(f"project not found: {ctx.run.project_id}")
    return project


def _definition(project: Project) -> dict[str, Any]:
    return project.definition if isinstance(project.definition, dict) else {}


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


async def _existing_keys(
    session: AsyncSession, project_id: uuid.UUID
) -> tuple[set[str], set[str], set[str]]:
    """项目内去重键：arxiv_id / doi / 标题小写。"""
    rows = (
        await session.execute(
            select(Paper.arxiv_id, Paper.doi, Paper.title).where(Paper.project_id == project_id)
        )
    ).all()
    arxiv_ids = {a for a, _, _ in rows if a}
    dois = {d.lower() for _, d, _ in rows if d}
    titles = {t.strip().lower() for _, _, t in rows if t}
    return arxiv_ids, dois, titles


# ---- 1. 检索候选（arXiv） ----

_WS_RE = re.compile(r"\s+")


def _normalize_kw(text: str) -> str:
    """归一化用于宽松子串匹配：小写、连字符→空格、压缩空白。

    这样 "Computer-Use Agents" 与关键词 "Computer Use Agent" 能互相命中。
    """
    return _WS_RE.sub(" ", text.lower().replace("-", " ")).strip()


def _keyword_match(entry: dict[str, Any], includes_norm: list[str]) -> bool:
    """标题+摘要归一化后，任一关键词（已归一化）作为子串命中即留；无关键词则全留。"""
    if not includes_norm:
        return True
    hay = _normalize_kw(f"{entry.get('title') or ''} {entry.get('abstract') or ''}")
    return any(kw in hay for kw in includes_norm)


@register("wiki.search_candidates")
async def search_candidates(ctx: ActionContext, params: dict[str, Any]) -> dict[str, Any]:
    knobs = _knobs(ctx)
    now = utcnow()
    mode = _mode(ctx)
    async with get_sessionmaker()() as session:
        project = await _get_project(session, ctx)
        definition = _definition(project)
        keywords_def = definition.get("keywords") or {}
        # 稀疏 definition 容忍：无 arxiv_categories 时回退默认 cs.* 分类
        categories = list(keywords_def.get("arxiv_categories") or []) or list(
            DEFAULT_ARXIV_CATEGORIES
        )
        include = list(keywords_def.get("include") or [])

        watermark = _parse_iso((project.ingest_state or {}).get("watermark"))
        if mode == "incremental" and watermark is not None:
            # 回看窗口覆盖 arXiv 关键词索引滞后，防止近几天的新论文被漏抓
            since = watermark - timedelta(days=_INCREMENTAL_LOOKBACK_DAYS)
        else:
            since = now - timedelta(days=30 * int(knobs["months_back"]))

        if _unlimited(knobs):
            limit = _UNLIMITED_SENTINEL  # 窗口内全量抓取（哨兵防失控）
        else:
            limit = min(_MAX_CANDIDATES_CAP, max(int(knobs["max_papers"]) * 3, 10))
        entries = await get_arxiv_client().search(
            categories=categories, keywords=include, since=since, until=now, limit=limit
        )

        arxiv_ids, dois, titles = await _existing_keys(session, project.id)
        new_papers: list[Paper] = []

        def _try_insert(entry: dict[str, Any], source: str) -> bool:
            """按 arxiv_id/doi/title 三方去重后入库；命中已存量返回 False。

            边插边更新去重键集合——RSS↔API↔存量共用同一套集合，互不重插。
            """
            aid = entry.get("arxiv_id")
            title = (entry.get("title") or "").strip()
            doi = (entry.get("doi") or "").lower() or None
            if not title:
                return False
            if (aid and aid in arxiv_ids) or (doi and doi in dois) or title.lower() in titles:
                return False
            paper = Paper(
                project_id=project.id,
                source=source,
                arxiv_id=aid,
                doi=entry.get("doi"),
                external_ids=({"arxiv": aid} | ({"doi": entry["doi"]} if entry.get("doi") else {})),
                title=title,
                authors=entry.get("authors"),
                abstract=entry.get("abstract"),
                year=entry.get("year"),
                venue=entry.get("primary_category"),
                url=entry.get("url"),
                published_at=_parse_iso(entry.get("published")),
                status="candidate",
            )
            session.add(paper)
            new_papers.append(paper)
            if aid:
                arxiv_ids.add(aid)
            if doi:
                dois.add(doi)
            titles.add(title.lower())
            return True

        # 补漏层：关键词检索的日期窗口（索引有 3-5 天滞后，故靠 RSS 补新鲜）
        for entry in entries:
            _try_insert(entry, "arxiv")

        # 新鲜源：分类 RSS /new 当天公告（即时无滞后），仅增量模式补最新论文
        rss_found = 0
        rss_matched = 0
        rss_inserted = 0
        rss_categories: list[str] = []
        if mode == "incremental":
            includes_norm = [n for k in include if (n := _normalize_kw(k))]
            for cat in categories:
                rss_entries = await get_arxiv_client().fetch_new(cat)
                rss_categories.append(cat)
                rss_found += len(rss_entries)
                for entry in rss_entries:
                    if not _keyword_match(entry, includes_norm):
                        continue
                    rss_matched += 1
                    if _try_insert(entry, "arxiv"):
                        rss_inserted += 1

        await session.flush()  # 拿到新论文 id，供 observation 清单
        brief = _paper_brief(new_papers)
        await session.commit()

    # 新水位线 = 本次检索时刻，由 wiki.update_watermark 落库
    ctx.checkpoint["watermark_candidate"] = now.isoformat()
    return {
        "found": len(entries),
        "inserted": len(new_papers),
        "new_papers": brief,
        "window_since": since.isoformat(),
        "mode": mode,
        "rss_found": rss_found,
        "rss_matched": rss_matched,
        "rss_inserted": rss_inserted,
        "rss_categories": rss_categories,
    }


# ---- 2. 引文雪球（Semantic Scholar） ----


@register("wiki.snowball")
async def snowball(ctx: ActionContext, params: dict[str, Any]) -> dict[str, Any]:
    knobs = _knobs(ctx)
    depth = max(0, min(2, int(knobs["snowball_depth"])))
    if depth == 0:
        return {"skipped": True, "reason": "snowball_depth=0"}

    s2 = get_s2_client()
    failed: list[dict[str, str]] = []
    new_papers: list[Paper] = []
    inserted = 0
    # 最大化模式：扩展新增篇数同样放开（哨兵防失控）；种子广度（锚点+最新 10 篇候选、
    # 下一层 frontier 取 10）仍为固定设计常量，控制的是 S2 API 调用量而非入库篇数
    max_new = _UNLIMITED_SENTINEL if _unlimited(knobs) else int(knobs["max_papers"]) * 2

    async with get_sessionmaker()() as session:
        project = await _get_project(session, ctx)
        definition = _definition(project)
        anchors = [
            normalize_arxiv_id(str(a.get("arxiv_id")))
            for a in (definition.get("anchor_papers") or [])
            if isinstance(a, dict) and a.get("arxiv_id")
        ]
        candidate_ids = (
            (
                await session.execute(
                    select(Paper.arxiv_id)
                    .where(
                        Paper.project_id == project.id,
                        Paper.status == "candidate",
                        Paper.arxiv_id.is_not(None),
                    )
                    .order_by(Paper.published_at.desc().nulls_last())
                    .limit(10)
                )
            )
            .scalars()
            .all()
        )
        frontier: list[str] = list(dict.fromkeys(anchors + list(candidate_ids)))
        arxiv_ids, dois, titles = await _existing_keys(session, project.id)
        processed_seeds = 0

        for _level in range(depth):
            next_frontier: list[str] = []
            for seed in frontier:
                if inserted >= max_new:
                    break
                processed_seeds += 1
                try:
                    refs = await s2.get_references(f"arXiv:{seed}")
                    cits = await s2.get_citations(f"arXiv:{seed}")
                except asyncio.CancelledError:
                    raise
                except Exception as e:  # noqa: BLE001 — 单个种子失败不打断批处理
                    failed.append({"id": seed, "error": f"{type(e).__name__}: {e}"})
                    continue
                for item in refs + cits:
                    if inserted >= max_new:
                        break
                    ext = item.get("externalIds") or {}
                    aid = normalize_arxiv_id(str(ext["ArXiv"])) if ext.get("ArXiv") else None
                    doi = str(ext.get("DOI")).lower() if ext.get("DOI") else None
                    title = (item.get("title") or "").strip()
                    if not title or title.lower() in titles:
                        continue
                    if (aid and aid in arxiv_ids) or (doi and doi in dois):
                        continue
                    paper = Paper(
                        project_id=project.id,
                        source="semantic_scholar",
                        arxiv_id=aid,
                        doi=ext.get("DOI"),
                        external_ids={
                            k: v
                            for k, v in (
                                ("s2", item.get("paperId")),
                                ("arxiv", aid),
                                ("doi", ext.get("DOI")),
                            )
                            if v
                        },
                        title=title,
                        authors=[
                            {"name": a.get("name")}
                            for a in (item.get("authors") or [])
                            if a.get("name")
                        ]
                        or None,
                        abstract=item.get("abstract"),
                        year=item.get("year"),
                        venue=item.get("venue") or None,
                        url=item.get("url"),
                        published_at=_parse_iso(item.get("publicationDate")),
                        status="candidate",
                    )
                    session.add(paper)
                    new_papers.append(paper)
                    inserted += 1
                    titles.add(title.lower())
                    if aid:
                        arxiv_ids.add(aid)
                        next_frontier.append(aid)
                    if doi:
                        dois.add(doi)
            frontier = next_frontier[:10]
            if not frontier:
                break
        await session.flush()  # 拿到新论文 id，供 observation 清单
        brief = _paper_brief(new_papers)
        await session.commit()

    return {
        "depth": depth,
        "processed": processed_seeds,
        "inserted": inserted,
        "new_papers": brief,
        "failed": failed,
    }


# ---- 3. 相关性打分（LLM stage=relevance） ----


@register("wiki.score_relevance")
async def score_relevance(ctx: ActionContext, params: dict[str, Any]) -> dict[str, Any]:
    knobs = _knobs(ctx)
    threshold = float(knobs["relevance_threshold"])

    async with get_sessionmaker()() as session:
        project = await _get_project(session, ctx)
        # 稀疏 definition 容忍：rubric / questions 缺失时 prompt 只用 statement
        context_text = build_relevance_context(project)

        # 幂等断点：只取仍是 candidate 的论文 id（已打分的不重复调 LLM）。外层只查 id，
        # 每篇打分在各自独立 session 内重新加载论文，避免多任务共享一个 AsyncSession。
        paper_ids = list(
            (
                await session.execute(
                    select(Paper.id)
                    .where(Paper.project_id == project.id, Paper.status == "candidate")
                    .order_by(Paper.published_at.desc().nulls_last(), Paper.created_at)
                    # 最大化模式打分不截断（全部 candidate 逐篇打分，哨兵防失控）
                    .limit(_UNLIMITED_SENTINEL if _unlimited(knobs) else _MAX_CANDIDATES_CAP)
                )
            ).scalars()
        )

    guidance = ctx.skill_guidance("wiki.score_relevance")
    total = len(paper_ids)
    progress = {"n": 0}

    async def score_one(paper_id: uuid.UUID) -> dict[str, Any] | None:
        """单篇打分：独立 session 重新加载 → 共享打分服务写字段 → 状态转移 → 自行 commit。"""
        async with get_sessionmaker()() as session:
            paper = await session.get(Paper, paper_id)
            if paper is None or paper.status != "candidate":
                return None  # 竞态/已处理：幂等跳过（正常流不会命中）
            progress["n"] += 1
            await ctx.log(f"相关性打分 {progress['n']}/{total}：{paper.title[:60]}")
            scored = await score_paper_relevance(
                paper,
                context_text=context_text,
                llm=ctx.llm,
                extra_guidance=guidance,
                user_id=ctx.run.created_by,
                voyage_id=ctx.run.id,
            )
            score = scored.score
            paper.status = "scored" if score >= threshold else "excluded"
            paper.trash_reason = None if paper.status == "scored" else "irrelevant"
            # 逐篇 commit：worker 中途被杀后按 status 断点续跑，不重复打分
            await session.commit()
            return {
                "id": str(paper.id),
                "title": paper.title,
                "score": score,
                "passed": paper.status == "scored",
            }

    results = await _gather_bounded(_LLM_CONCURRENCY, [score_one(pid) for pid in paper_ids])
    _reraise_if_cancelled(results)  # worker 被杀须上抛，不当作单篇失败

    # gather 后统一汇总（顺序按查询顺序，稳定；不在并发任务里改 ctx.checkpoint）
    succeeded = 0
    excluded = 0
    failed: list[dict[str, str]] = []
    scored_ids: list[str] = []
    scored_brief: list[dict[str, Any]] = []
    for paper_id, result in zip(paper_ids, results, strict=True):
        if isinstance(result, BaseException):
            failed.append({"id": str(paper_id), "error": f"{type(result).__name__}: {result}"})
            continue
        if result is None:
            continue
        succeeded += 1
        scored_ids.append(result["id"])
        if not result["passed"]:
            excluded += 1
        if len(scored_brief) < _OBS_LIST_CAP:
            scored_brief.append(result)

    # 步内进度记入 checkpoint（审计用；幂等本身靠 Paper.status）
    ctx.checkpoint["scored_ids"] = list(ctx.checkpoint.get("scored_ids") or []) + scored_ids
    return {
        "processed": len(scored_ids) + len(failed),
        "succeeded": succeeded,
        "excluded": excluded,
        "threshold": threshold,
        "scored_papers": scored_brief,
        "failed": failed,
    }


# ---- 4. 下载 PDF + 抽全文（PyMuPDF，失败降级 abstract） ----


def _compile_limit(knobs: dict[str, Any]) -> int:
    # 最大化模式：全部达标论文进入抽取/编译（compile_top_n/max_papers 被忽略，哨兵防失控）
    if _unlimited(knobs):
        return _UNLIMITED_SENTINEL
    return max(1, min(int(knobs["compile_top_n"]), int(knobs["max_papers"])))


@register("wiki.fetch_extract")
async def fetch_extract(ctx: ActionContext, params: dict[str, Any]) -> dict[str, Any]:
    knobs = _knobs(ctx)
    top_n = _compile_limit(knobs)
    arxiv = get_arxiv_client()
    fetched = 0
    degraded = 0
    failed: list[dict[str, str]] = []

    async with get_sessionmaker()() as session:
        papers = (
            (
                await session.execute(
                    select(Paper)
                    .where(Paper.project_id == ctx.run.project_id, Paper.status == "scored")
                    .order_by(Paper.relevance_score.desc().nulls_last())
                    .limit(top_n)
                )
            )
            .scalars()
            .all()
        )
        # 发表日期回填：雪球/DOI 来源的论文只有年份，arXiv 能查到精确日期（时间线/趋势视图用）
        need_dates = [p for p in papers if p.published_at is None and p.arxiv_id]
        if need_dates:
            try:
                # 分批查（每批 100）：最大化模式下待回填论文可达数百上千，单次 id_list
                # 会超 URL 长度/arXiv 单请求上限
                entries = []
                for i in range(0, len(need_dates), 100):
                    entries.extend(
                        await arxiv.fetch_by_ids([p.arxiv_id for p in need_dates[i : i + 100]])
                    )
                by_aid = {e.get("arxiv_id"): e for e in entries if e.get("arxiv_id")}
                for p in need_dates:
                    entry = by_aid.get(p.arxiv_id)
                    if entry:
                        p.published_at = _parse_iso(entry.get("published"))
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — 日期回填尽力而为
                logger.warning("published_at backfill failed", exc_info=True)
        for paper in papers:
            try:
                if paper.full_text_path is None and paper.arxiv_id:
                    content = await arxiv.download_pdf(paper.arxiv_id)
                    pdf_path = save_pdf(str(paper.id), content)
                    txt_path = await extract_full_text(str(paper.id), pdf_path)
                    paper.pdf_path = str(pdf_path)
                    paper.full_text_path = str(txt_path)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 — 下载/抽取失败降级用 abstract
                degraded += 1
                failed.append({"id": str(paper.id), "error": f"{type(e).__name__}: {e}"})
            # 发表机构补充（OpenAlex 反查，高级检索用）；失败不影响主流程
            if paper.affiliations is None and (paper.arxiv_id or paper.doi):
                try:
                    meta = (
                        await get_openalex_client().get_by_arxiv(paper.arxiv_id)
                        if paper.arxiv_id
                        else await get_openalex_client().get_by_doi(paper.doi)
                    )
                    paper.affiliations = (meta or {}).get("affiliations") or []
                    # 顺带补发表日期（无 arxiv_id 的 DOI 论文走不了上面的 arXiv 回填）
                    if paper.published_at is None:
                        paper.published_at = _parse_iso((meta or {}).get("published"))
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 — 机构补充尽力而为
                    logger.warning("affiliation enrich failed for %s", paper.id, exc_info=True)
            # 全文分段索引（文献问答/idea 生成的知识底座）；失败不影响主流程
            if paper.full_text_path:
                try:
                    await index_paper_fulltext(session, paper)
                except asyncio.CancelledError:
                    raise
                except Exception as e:  # noqa: BLE001
                    failed.append(
                        {"id": str(paper.id), "error": f"chunks: {type(e).__name__}: {e}"}
                    )
            # 顺带提取候选图（基础信息 caption=null；筛选注释在 wiki.compile 后做）；
            # 失败不影响全文流程
            if paper.pdf_path and paper.figures is None:
                try:
                    candidates = await extract_figures(str(paper.id), Path(paper.pdf_path))
                    paper.figures = [c | {"caption": None, "important": False} for c in candidates]
                except asyncio.CancelledError:
                    raise
                except Exception as e:  # noqa: BLE001
                    failed.append(
                        {"id": str(paper.id), "error": f"figures: {type(e).__name__}: {e}"}
                    )
            paper.status = "fetched"  # 无全文也进入编译（Librarian 退化用摘要）
            await session.commit()
            fetched += 1

    return {
        "processed": len(papers),
        "succeeded": fetched,
        "degraded": degraded,
        "fetched_papers": [
            {"id": str(p.id), "title": p.title, "pdf": bool(p.pdf_path)}
            for p in papers[:_OBS_LIST_CAP]
        ],
        "failed": failed,
    }


# ---- 5. Librarian 图文编译（LLM stage=librarian 多模态，全文优先，中文 wiki 页） ----


@register("wiki.compile")
async def compile_wiki(ctx: ActionContext, params: dict[str, Any]) -> dict[str, Any]:
    knobs = _knobs(ctx)
    top_n = _compile_limit(knobs)

    async with get_sessionmaker()() as session:
        project = await _get_project(session, ctx)
        statement = _definition(project).get("statement") or project.name
        # 幂等断点：已 compiled 的不再进入（status=fetched 才编译）。外层只查 id，每篇
        # 编译在各自独立 session 内重新加载论文，避免多任务共享一个 AsyncSession。
        paper_ids = list(
            (
                await session.execute(
                    select(Paper.id)
                    .where(Paper.project_id == project.id, Paper.status == "fetched")
                    .order_by(Paper.relevance_score.desc().nulls_last())
                    .limit(top_n)
                )
            ).scalars()
        )

    guidance = ctx.skill_guidance("wiki.compile")
    total = len(paper_ids)
    progress = {"n": 0}

    async def compile_one(paper_id: uuid.UUID) -> dict[str, Any] | None:
        """单篇编译：独立 session 重新加载 → 挑图注释 → 图文编译 → 自行 commit。"""
        async with get_sessionmaker()() as session:
            paper = await session.get(Paper, paper_id)
            if paper is None or paper.status != "fetched":
                return None  # 竞态/已处理：幂等跳过（正常流不会命中）
            progress["n"] += 1
            await ctx.log(f"📖 精读编译 {progress['n']}/{total}：{paper.title}")
            # ① 编译前筛选注释论文图（stage=librarian 多模态）：图文编译要用重要图；
            #    失败仅 log（annotate 内部已带降级），不影响编译。annotate 只改内存对象，
            #    由本任务的 session commit。
            if paper.figures and not figures_annotated(paper.figures):
                try:
                    await annotate_figures(
                        paper,
                        paper.figures,
                        llm=ctx.llm,
                        user_id=ctx.run.created_by,
                        voyage_id=ctx.run.id,
                    )
                    await session.commit()
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001
                    logger.warning("figure annotation failed for paper %s", paper.id, exc_info=True)
            # ② 图文编译（重要图 ≤4 张随 prompt 送入）+ ③ 无效 ![[fig:N]] 标记剥除
            compiled = await compile_paper(
                paper,
                statement=statement,
                llm=ctx.llm,
                user_id=ctx.run.created_by,
                voyage_id=ctx.run.id,
                extra_guidance=guidance,
            )
            paper.wiki_content = compiled.content
            paper.compiled_at = utcnow()
            paper.compiled_model = compiled.model or None
            paper.status = "compiled"
            await session.commit()
            await ctx.log(
                f"✓ 完成 {progress['n']}/{total}：{paper.title[:50]}（{len(compiled.content)} 字）",
                level="success",
            )
            return {"id": str(paper.id), "title": paper.title}

    results = await _gather_bounded(_LLM_CONCURRENCY, [compile_one(pid) for pid in paper_ids])
    _reraise_if_cancelled(results)  # worker 被杀须上抛，不当作单篇失败

    # gather 后统一汇总（顺序按查询顺序=相关度降序，稳定）
    compiled = 0
    compiled_brief: list[dict[str, str]] = []
    failed: list[dict[str, str]] = []
    for paper_id, result in zip(paper_ids, results, strict=True):
        if isinstance(result, BaseException):
            failed.append({"id": str(paper_id), "error": f"{type(result).__name__}: {result}"})
            await ctx.log(f"✗ 编译失败：{paper_id} — {type(result).__name__}", level="error")
            continue
        if result is None:
            continue
        compiled += 1
        if len(compiled_brief) < _OBS_LIST_CAP:
            compiled_brief.append(result)

    ctx.checkpoint["compiled_count"] = int(ctx.checkpoint.get("compiled_count") or 0) + compiled
    return {
        "processed": len(paper_ids),
        "succeeded": compiled,
        "compiled_papers": compiled_brief,
        "failed": failed,
    }


# ---- 6. 概念上链 + embedding ----


@register("wiki.link_concepts")
async def link_concepts(ctx: ActionContext, params: dict[str, Any]) -> dict[str, Any]:
    async with get_sessionmaker()() as session:
        project = await _get_project(session, ctx)
        # 全库上链逻辑与手动补建端点共用（services/concepts.py）
        stats, papers = await link_all_paper_concepts(
            session,
            project_id=project.id,
            llm=ctx.llm,
            user_id=ctx.run.created_by,
            voyage_id=ctx.run.id,
        )
        created = int(stats["concepts_created"])
        new_names = list(stats["new_concepts"])
        new_links = int(stats["links_created"])

        # embedding：编译完成且尚无向量的论文批量嵌入（provider 不支持则跳过）
        embedded = 0
        embed_error: str | None = None
        pending = [p for p in papers if p.embedding is None]
        if pending:
            texts = [f"{p.title}\n{p.tldr or ''}\n{p.abstract or ''}"[:2000] for p in pending]
            try:
                vectors = await ctx.llm.embed(
                    texts,
                    user_id=ctx.run.created_by,
                    project_id=ctx.run.project_id,
                    voyage_id=ctx.run.id,
                )
                for paper, vector in zip(pending, vectors, strict=True):
                    paper.embedding = vector
                    embedded += 1
                await session.commit()
            except asyncio.CancelledError:
                raise
            except NotImplementedError:
                embed_error = "provider does not support embeddings"
            except Exception as e:  # noqa: BLE001 — 嵌入失败不影响上链结果
                embed_error = f"{type(e).__name__}: {e}"

        # 全文分段向量：补齐缺失的 chunk embedding（文献问答检索底座）。
        # 最大化模式放开单次 2000 段的默认上限（否则大批量编译后向量长期欠账）
        embed_kwargs: dict[str, Any] = (
            {"limit": _UNLIMITED_CHUNK_SENTINEL} if _unlimited(_knobs(ctx)) else {}
        )
        chunks_embedded, chunk_embed_error = await embed_pending_chunks(
            session,
            project_id=project.id,
            llm=ctx.llm,
            user_id=ctx.run.created_by,
            voyage_id=ctx.run.id,
            **embed_kwargs,
        )

    return {
        "papers": len(papers),
        "concepts_created": created,
        "new_concepts": new_names[:_OBS_LIST_CAP],
        "links_created": new_links,
        "links_removed": int(stats["links_removed"]),
        "concepts_removed": int(stats["concepts_removed"]),
        "embedded": embedded,
        "embed_error": embed_error,
        "chunks_embedded": chunks_embedded,
        "chunk_embed_error": chunk_embed_error,
    }


# ---- 7. 更新水位线 + 活动流 ----


@register("wiki.update_watermark")
async def update_watermark(ctx: ActionContext, params: dict[str, Any]) -> dict[str, Any]:
    async with get_sessionmaker()() as session:
        project = await _get_project(session, ctx)
        state = dict(project.ingest_state or {})
        watermark = ctx.checkpoint.get("watermark_candidate") or state.get("watermark")
        finished_at = utcnow().isoformat()
        state["watermark"] = watermark
        state["last_run"] = {"voyage_id": str(ctx.run.id), "finished_at": finished_at}
        project.ingest_state = state

        compiled_count = int(ctx.checkpoint.get("compiled_count") or 0)
        session.add(
            Activity(
                project_id=project.id,
                actor="agent:librarian",
                kind="ingest.completed",
                message=f"文献调研完成：本次编译 {compiled_count} 篇 wiki 页",
                payload={
                    "voyage_id": str(ctx.run.id),
                    "mode": _mode(ctx),
                    "compiled": compiled_count,
                    "watermark": watermark,
                },
            )
        )
        await session.commit()

    return {"watermark": watermark, "compiled": int(ctx.checkpoint.get("compiled_count") or 0)}
