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
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.voyage.actions import ActionContext, register
from app.agents.voyage.actions_ideas import cosine_similarity
from app.core.db import get_sessionmaker
from app.core.embedding_space import active_space
from app.models.activity import Activity
from app.models.base import utcnow
from app.models.daily_feed import DailyFeedEntry
from app.models.library_direction import DirectionLibrary, LibraryPaper
from app.models.paper import Paper, new_paper
from app.models.voyage import VoyageStep
from app.schemas.ingest import TIME_RANGE_DAYS
from app.services.affiliations import (
    apply_author_affiliations,
    extract_author_affiliations_llm,
    get_affiliation_extraction_mode,
)
from app.services.chunks import (
    embed_pending_chunks,
    ensure_paper_chunks,
    sync_abstract_chunk_vectors,
)
from app.services.concepts import link_all_paper_concepts
from app.services.dedup import pool_dedup_key
from app.services.embedding import (
    embed_documents,
    embed_query,
    paper_vectors,
    papers_with_vector,
    upsert_paper_vector,
)
from app.services.figure_annotate import annotate_figures, figures_annotated
from app.services.libraries import (
    ensure_membership,
    find_pool_paper,
    get_library_for_project,
    library_definition,
    rejected_paper_ids,
    remember_rejected,
)
from app.services.literature import get_arxiv_client, get_openalex_client, get_s2_client
from app.services.literature.arxiv import normalize_arxiv_id
from app.services.literature.pdf_extract import extract_figures, extract_full_text, save_pdf
from app.services.paper_enrich import paper_embedding_text
from app.services.paper_wiki import upsert_wiki
from app.services.papers import delete_membership_hard
from app.services.projects import DEFAULT_ARXIV_CATEGORIES
from app.services.relevance import (
    build_direction_query,
    build_relevance_context,
    score_paper_relevance,
)
from app.services.wiki_compile import compile_paper

logger = logging.getLogger(__name__)

DEFAULT_KNOBS: dict[str, Any] = {
    "months_back": 6,
    "max_papers": 150,
    "relevance_threshold": 0.8,
    "snowball_depth": 1,
    "compile_top_n": 50,
    "unlimited": False,
}

# 检索/打分的安全上限。这是防失控的天花板，不是常规取数口径——常规口径是用户填的
# 「最大检索篇数」（max_papers，上限 500）。
_MAX_CANDIDATES_CAP = 500

# 最大化模式（knobs.unlimited=True）的安全哨兵：检索/打分/抽取/编译均按「窗口内全量」
# 处理，不再受 max_papers/compile_top_n/_MAX_CANDIDATES_CAP 截断；此哨兵只防真正失控
# （查询发散/外部 API 异常返回海量条目），正常调研窗口远达不到。
_UNLIMITED_SENTINEL = 10_000

# 最大化模式下单次运行补齐 chunk embedding 的哨兵上限（全量论文 × 每篇 ≤120 段）；
# 默认模式沿用 services/chunks.py 的 2000/次（剩余随每日同步补齐）。
_UNLIMITED_CHUNK_SENTINEL = 1_000_000


def _unlimited(knobs: dict[str, Any]) -> bool:
    return bool(knobs.get("unlimited"))


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
    """本次运行的收集模式：``search`` | ``snowball`` | ``incremental``。

    ``bootstrap`` 是 ``search`` 的旧名；存量任务的 checkpoint 里还写着它。
    """
    mode = _params(ctx).get("mode")
    if mode == "bootstrap":
        return "search"
    if mode in ("search", "snowball", "incremental"):
        return mode
    return "incremental" if ctx.run.kind == "wiki_ingest" else "search"


async def _resolve_library(session: AsyncSession, ctx: ActionContext) -> DirectionLibrary | None:
    """P9a：库化任务优先按 ``run.library_id`` 直接定位方向库；回退起源课题解析（兼容
    库化前建的存量 run）。独立库（无起源课题）也能由此拿到自己的库。"""
    if ctx.run.library_id is not None:
        library = await session.get(DirectionLibrary, ctx.run.library_id)
        if library is not None:
            return library
    if ctx.run.project_id is not None:
        return await get_library_for_project(session, ctx.run.project_id)
    return None


def _ingest_billing_owner(library: DirectionLibrary) -> uuid.UUID | None:
    """ingest LLM 调用的计费归属（P10）：公共库走全局/系统 key（None），个人库走
    创建者 key（submitted_by）。token 记账仍按 library_id 落库（另经调用参数带上）。"""
    return None if library.is_public else library.submitted_by


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


async def _existing_keys(
    session: AsyncSession, library_id: uuid.UUID
) -> tuple[set[str], set[str], set[str]]:
    """库内去重键：arxiv_id / doi / 标题小写（本库已有成员的论文不再重复入库）。"""
    rows = (
        await session.execute(
            select(Paper.arxiv_id, Paper.doi, Paper.title)
            .join(LibraryPaper, LibraryPaper.paper_id == Paper.id)
            .where(LibraryPaper.library_id == library_id)
        )
    ).all()
    arxiv_ids = {a for a, _, _ in rows if a}
    dois = {d.lower() for _, d, _ in rows if d}
    titles = {t.strip().lower() for _, _, t in rows if t}
    return arxiv_ids, dois, titles


# Postgres 在并发写下检测到环就挑一个事务牺牲掉，这是正常行为而不是错误：
# 40P01 死锁 / 40001 序列化失败，两者都该重试而不是让整个步骤挂掉。
_RETRYABLE_SQLSTATES = frozenset({"40P01", "40001"})
_DEADLOCK_RETRIES = 3


def _is_retryable_conflict(exc: BaseException) -> bool:
    orig = getattr(exc, "orig", None)
    code = getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)
    return code in _RETRYABLE_SQLSTATES


async def _insert_pooled_with_retry(
    session: AsyncSession, **kwargs: Any
) -> tuple[Paper | None, str | None]:
    """插入一篇池论文并**立刻提交**；撞上并发冲突就回滚重试。返回 (论文, 放弃原因)。

    为什么逐篇提交：整步一个事务时，每插一行都会在 ``papers.dedup_key`` 唯一索引上
    持锁到步骤结束（实测能到几分钟）。而各库是并行同步的、方向又都在 AI 领域，
    Semantic Scholar 返回的引文大量重叠、插入顺序还各不相同——两个事务互等对方
    未提交的行，正好成环。逐篇提交把锁窗口从几分钟压到毫秒级。

    真撞上了也只丢这一篇：重试三次仍冲突就记进 failed 继续跑，不再让整步作废、
    把已经拿到的上百篇引文一起丢掉。
    """
    for attempt in range(_DEADLOCK_RETRIES):
        try:
            paper = await _pool_paper_or_new(session, **kwargs)
            await session.commit()
            return paper, None
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 — 只吞冲突类，其余照抛
            await session.rollback()
            if not _is_retryable_conflict(e):
                raise
            if attempt == _DEADLOCK_RETRIES - 1:
                return None, f"{type(e).__name__}: {e}"
            logger.warning("snowball insert conflict, retry %d", attempt + 1)
            await asyncio.sleep(0.2 * (attempt + 1))
    return None, None


async def _pool_paper_or_new(
    session: AsyncSession,
    *,
    library_id: uuid.UUID,
    make_paper: Any,
    arxiv_id: str | None,
    doi: str | None,
    title: str,
    year: int | None,
    authors: list[Any] | None,
) -> Paper | None:
    """写路径统一入口：先按 dedup 键查全局内容池，命中只建成员行（pool hit，
    跳过重复解析），未命中用 ``make_paper()`` 建新内容池行 + 成员行。

    返回本次新加入库的 Paper；本库已有成员行时返回 None（跳过）。
    """
    key = pool_dedup_key(arxiv_id=arxiv_id, doi=doi, title=title, year=year, authors=authors)
    pooled = await find_pool_paper(session, arxiv_id=arxiv_id, doi=doi, dedup_key=key)
    if pooled is not None:
        _, created = await ensure_membership(
            session, library_id=library_id, paper_id=pooled.id, status="candidate"
        )
        if not created:
            return None
        logger.info("paper pool hit: %s (%s)", pooled.id, title[:60])
        return pooled
    paper = make_paper()
    paper.dedup_key = key
    session.add(paper)
    await session.flush()
    await ensure_membership(session, library_id=library_id, paper_id=paper.id, status="candidate")
    return paper


# ---- 1. 检索候选（arXiv） ----


async def _rank_feed_by_similarity(
    ctx: ActionContext,
    session: AsyncSession,
    *,
    direction: str,
    papers: list[Paper],
    limit: int,
) -> tuple[list[Paper], bool]:
    """按与研究方向的向量相似度给每日池论文粗排，取前 ``limit``。

    ``direction`` 由 :func:`build_direction_query` 组好（方向描述 + 收录关键词）——
    只用 statement 的话，方向描述写得含糊的库会被排得很差，而关键词恰恰是这类库表达
    意图的主要方式。

    返回 (排序后的论文, 是否真的排过序)。**这是排序不是过滤**——嵌入不可用时原样返回，
    宁可多送几篇去打分，也不能因为向量缺失让这个库一篇都收不到。

    只跟激活空间下的论文向量比：方向向量与论文向量必须出自同一个模型，否则这个粗排
    就是在按噪声挑论文，而且不会有任何报错。

    在 Python 里算余弦而不是走 pgvector：每日池只有几百行，跨 sqlite/postgres 一致，
    测试路径不用特判。
    """
    if not direction.strip():
        return papers[:limit], False
    try:
        vector, space = await embed_query(
            session,
            direction[:2000],
            user_id=ctx.run.created_by,
            project_id=ctx.run.project_id,
            library_id=ctx.run.library_id,
            voyage_id=ctx.run.id,
        )
    except Exception:  # noqa: BLE001 — provider 不支持嵌入 / 尚无向量空间：退回不排序
        logger.warning("feed ranking embed failed for library %s", ctx.run.library_id)
        return papers[:limit], False
    vectors = await paper_vectors(session, [p.id for p in papers], space)
    if not vectors:
        return papers[:limit], False
    scored = [(cosine_similarity(vector, vectors[p.id]), p) for p in papers if p.id in vectors]
    scored.sort(key=lambda x: -x[0])
    ranked = [p for _, p in scored]
    # 没建向量的排在后面而不是丢掉（同上：粗排不该有排除语义）
    ranked.extend(p for p in papers if p.id not in vectors)
    return ranked[:limit], True


async def _collect_from_daily_feed(
    ctx: ActionContext,
    session: AsyncSession,
    *,
    library: DirectionLibrary,
    direction: str,
    exclude: list[str],
    limit: int,
) -> dict[str, Any]:
    """增量同步的候选来源：每日论文池，不碰 arXiv。

    取**整张 daily_feed_entries**、不加时间窗——``cleanup_expired`` 已经保证这张表里
    只有保留期内的行，表本身就是那个窗口。全量重扫是有意的：上次同步失败/欠费/被暂停
    之后，下一次会自动把落下的补上，不会「漏一天就永久丢失」。

    重复送打分由 :func:`_existing_keys` 挡掉——它不按 status 过滤，所以昨天已判
    ``excluded`` 的论文今天不会再花一次 LLM。

    **不按分类、也不按「包括关键词」筛**：它们只在检索模式下用来构造 arXiv 查询。两者
    都是硬门槛，配窄了会让库悄无声息颗粒无收，而「0 篇」和「今天确实没有相关论文」在
    界面上无法区分。

    **「排除关键词」是例外，这里会硬过滤。** 那是用户明确说「我不要这个」，漏掉它是
    失职而不是保守；过滤掉多少篇会记进观测，不至于无声无息。
    """
    # 扫描范围（管理员可配，默认 since_last）：
    # - since_last：从本库上次成功同步那天算起。正常就是当天那批（省），漏了几天会
    #   自动多扫几天（能自愈）——这两件事本来要在 daily 和 full 之间二选一。
    # - daily：只扫当天。最省，但漏掉的永久错过（arXiv 不会再给第二次）。
    # - full：扫整张表。最稳，代价是重复排序几千篇早就处理过的。
    from app.services.daily_feed import get_sync_scope

    scope = await get_sync_scope(session)
    stmt = (
        select(Paper, DailyFeedEntry.feed_date)
        .join(DailyFeedEntry, DailyFeedEntry.paper_id == Paper.id)
        .order_by(DailyFeedEntry.feed_date.desc())
    )
    since_day: Any = None
    if scope == "daily":
        since_day = datetime.now(UTC).date()
        stmt = stmt.where(DailyFeedEntry.feed_date == since_day)
    elif scope == "since_last":
        last = _parse_iso(((library.ingest_state or {}).get("last_run") or {}).get("finished_at"))
        # 没有上次记录（首次同步）就按全量走：宁可多扫一轮，也不能开局就漏
        if last is not None:
            since_day = last.astimezone(UTC).date()
            stmt = stmt.where(DailyFeedEntry.feed_date >= since_day)
    rows = (await session.execute(stmt)).all()
    feed_latest = max((d for _, d in rows), default=None)
    # 判过且没通过的直接跳过：它们的成员行已被删掉，去重集合挡不住，而扫描窗口
    # 和上次同步那天是重叠的——不挡的话今天淘汰的几百篇明天会再打一次分。
    rejected = rejected_paper_ids(library)
    feed_papers = [p for p, _ in rows if str(p.id) not in rejected]
    skipped_rejected = len(rows) - len(feed_papers)

    excluded_by_terms = 0
    if exclude:
        norm = [e.lower() for e in exclude if e.strip()]
        kept: list[Paper] = []
        for paper in feed_papers:
            hay = f"{paper.title or ''} {paper.abstract or ''}".lower()
            if any(term in hay for term in norm):
                excluded_by_terms += 1
                continue
            kept.append(paper)
        feed_papers = kept

    ranked, did_rank = await _rank_feed_by_similarity(
        ctx, session, direction=direction, papers=feed_papers, limit=limit
    )

    arxiv_ids, dois, titles = await _existing_keys(session, library.id)
    selected: list[Paper] = []
    already = 0
    for paper in ranked:
        aid = paper.arxiv_id
        title = (paper.title or "").strip()
        doi = (paper.doi or "").lower() or None
        if not title:
            continue
        if (aid and aid in arxiv_ids) or (doi and doi in dois) or title.lower() in titles:
            already += 1
            continue
        # 论文已经在内容池里（每日推送建的就是池论文），只需要补一条成员行。
        # 以 created 为准而不是只信上面的去重集合：那三个集合按 arxiv_id/doi/标题建，
        # 标题为空的存量论文一个都进不去，只靠集合会把「早就在库里」误报成新增。
        _, created = await ensure_membership(
            session, library_id=library.id, paper_id=paper.id, status="candidate"
        )
        if not created:
            already += 1
            continue
        if aid:
            arxiv_ids.add(aid)
        if doi:
            dois.add(doi)
        titles.add(title.lower())
        selected.append(paper)

    return {
        "feed_total": len(rows),
        "feed_scope": scope,
        "skipped_previously_rejected": skipped_rejected,
        "feed_since": since_day.isoformat() if since_day else None,
        "excluded_by_terms": excluded_by_terms,
        "feed_ranked": did_rank,
        "after_vector_rank": len(ranked),
        "already_in_library": already,
        "selected": selected,
        "feed_latest_date": feed_latest.isoformat() if feed_latest else None,
    }


@register("wiki.search_candidates")
async def search_candidates(ctx: ActionContext, params: dict[str, Any]) -> dict[str, Any]:
    knobs = _knobs(ctx)
    now = utcnow()
    mode = _mode(ctx)
    async with get_sessionmaker()() as session:
        library = await _resolve_library(session, ctx)
        if library is None:
            raise ValueError(f"library not found for project: {ctx.run.project_id}")
        # P8a：收录配置读库自身 definition（不再读起源课题 project.definition）
        definition = library_definition(library)
        direction_query = build_direction_query(definition, library.name)
        keywords_def = definition.get("keywords") or {}
        # 稀疏 definition 容忍：无 arxiv_categories 时回退默认 cs.* 分类
        categories = list(keywords_def.get("arxiv_categories") or []) or list(
            DEFAULT_ARXIV_CATEGORIES
        )
        include = list(keywords_def.get("include") or [])
        # 排除词与包括词不同：包括词配窄了会让库悄无声息收不到东西，所以同步时不拿它筛；
        # 排除词是用户明确说「我不要这个」，漏掉它反而是失职，三条路径一律生效。
        exclude = list(keywords_def.get("exclude") or [])

        if _unlimited(knobs):
            limit = _UNLIMITED_SENTINEL  # 窗口内全量抓取（哨兵防失控）
        else:
            # 「最大检索篇数」就是检索上限本身。以前是 min(200, max_papers*3)，填 150
            # 实际会检索 200——旋钮名和行为对不上，用户按名字调参就调不准。
            limit = max(1, min(_MAX_CANDIDATES_CAP, int(knobs["max_papers"])))

        # —— 增量同步：候选一律来自每日论文池，不访问 arXiv ——
        # arXiv 检索是限流的主要来源（生产上三个库同步就是死在这个调用的 429 上），
        # 而每日推送本来就每天把新论文抓进内容池了，同一批论文没有理由抓第二遍。
        if mode == "incremental":
            feed = await _collect_from_daily_feed(
                ctx,
                session,
                library=library,
                direction=direction_query,
                exclude=exclude,
                limit=limit,
            )
            new_papers = feed.pop("selected")
            await session.flush()
            brief = _paper_brief(new_papers)
            await session.commit()
            ctx.checkpoint["watermark_candidate"] = now.isoformat()
            await ctx.log(
                f"每日池 {feed['feed_total']} 篇 → 粗排取 {feed['after_vector_rank']}"
                f" → 已在库 {feed['already_in_library']} → 送打分 {len(new_papers)}"
            )
            return {
                "source": "daily_feed",
                "mode": mode,
                "inserted": len(new_papers),
                "new_papers": brief,
                **feed,
            }

        # —— 检索模式：走 arXiv 检索 API（每日池只有当天公告、没有历史，回填不了）——
        # 查询词优先用本次指定的；没指定就退回库配置里的「包括关键词」
        params = _params(ctx)
        terms = [t for t in (params.get("query_terms") or []) if str(t).strip()] or include
        days = TIME_RANGE_DAYS.get(str(params.get("time_range") or ""))
        since = now - timedelta(days=days or 30 * int(knobs["months_back"]))
        entries = await get_arxiv_client().search(
            categories=categories,
            keywords=terms,
            exclude=exclude,
            since=since,
            until=now,
            limit=limit,
        )

        arxiv_ids, dois, titles = await _existing_keys(session, library.id)
        new_papers: list[Paper] = []

        async def _try_insert(entry: dict[str, Any], source: str) -> bool:
            """三方键（arxiv_id/doi/title）+ 全局内容池去重后入库；已在本库返回 False。

            边插边更新去重键集合——RSS↔API↔存量共用同一套集合，互不重插；
            池中已有的论文只建成员行（跳过重复解析）。
            """
            aid = entry.get("arxiv_id")
            title = (entry.get("title") or "").strip()
            doi = (entry.get("doi") or "").lower() or None
            if not title:
                return False
            if (aid and aid in arxiv_ids) or (doi and doi in dois) or title.lower() in titles:
                return False

            def make_paper() -> Paper:
                return new_paper(
                    source=source,
                    arxiv_id=aid,
                    doi=entry.get("doi"),
                    external_ids=(
                        {"arxiv": aid} | ({"doi": entry["doi"]} if entry.get("doi") else {})
                    ),
                    title=title,
                    authors=entry.get("authors"),
                    abstract=entry.get("abstract"),
                    year=entry.get("year"),
                    venue=entry.get("primary_category"),
                    url=entry.get("url"),
                    published_at=_parse_iso(entry.get("published")),
                )

            paper = await _pool_paper_or_new(
                session,
                library_id=library.id,
                make_paper=make_paper,
                arxiv_id=aid,
                doi=doi,
                title=title,
                year=entry.get("year"),
                authors=entry.get("authors"),
            )
            if aid:
                arxiv_ids.add(aid)
            if doi:
                dois.add(doi)
            titles.add(title.lower())
            if paper is None:
                return False
            new_papers.append(paper)
            return True

        for entry in entries:
            await _try_insert(entry, "arxiv")

        await session.flush()  # 拿到新论文 id，供 observation 清单
        brief = _paper_brief(new_papers)
        await session.commit()

    # 新水位线 = 本次检索时刻，由 wiki.update_watermark 落库
    ctx.checkpoint["watermark_candidate"] = now.isoformat()
    return {
        "source": "arxiv",
        "found": len(entries),
        "inserted": len(new_papers),
        "new_papers": brief,
        "window_since": since.isoformat(),
        "mode": mode,
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
        library = await _resolve_library(session, ctx)
        if library is None:
            raise ValueError(f"library not found for project: {ctx.run.project_id}")
        definition = library_definition(library)
        anchors = [
            normalize_arxiv_id(str(a.get("arxiv_id")))
            for a in (definition.get("anchor_papers") or [])
            if isinstance(a, dict) and a.get("arxiv_id")
        ]
        candidate_ids = (
            (
                await session.execute(
                    select(Paper.arxiv_id)
                    .join(LibraryPaper, LibraryPaper.paper_id == Paper.id)
                    .where(
                        LibraryPaper.library_id == library.id,
                        LibraryPaper.status == "candidate",
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
        arxiv_ids, dois, titles = await _existing_keys(session, library.id)
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
                    authors = [
                        {"name": a.get("name")}
                        for a in (item.get("authors") or [])
                        if a.get("name")
                    ] or None

                    def make_paper(
                        item: dict[str, Any] = item,
                        ext: dict[str, Any] = ext,
                        aid: str | None = aid,
                        title: str = title,
                        authors: list[Any] | None = authors,
                    ) -> Paper:
                        return new_paper(
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
                            authors=authors,
                            abstract=item.get("abstract"),
                            year=item.get("year"),
                            venue=item.get("venue") or None,
                            url=item.get("url"),
                            published_at=_parse_iso(item.get("publicationDate")),
                        )

                    paper, conflict = await _insert_pooled_with_retry(
                        session,
                        library_id=library.id,
                        make_paper=make_paper,
                        arxiv_id=aid,
                        doi=doi,
                        title=title,
                        year=item.get("year"),
                        authors=authors,
                    )
                    if conflict is not None:
                        failed.append({"id": title[:80], "error": conflict})
                    titles.add(title.lower())
                    if aid:
                        arxiv_ids.add(aid)
                        next_frontier.append(aid)
                    if doi:
                        dois.add(doi)
                    if paper is None:
                        continue
                    new_papers.append(paper)
                    inserted += 1
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
        library = await _resolve_library(session, ctx)
        if library is None:
            raise ValueError(f"library not found for project: {ctx.run.project_id}")
        # 稀疏 definition 容忍：rubric / questions 缺失时 prompt 只用 statement
        context_text = build_relevance_context(library_definition(library), library.name)
        billing_user_id = _ingest_billing_owner(library)

        # 幂等断点：只取仍是 candidate 的成员行（已打分的不重复调 LLM）。外层只查 id，
        # 每篇打分在各自独立 session 内重新加载，避免多任务共享一个 AsyncSession。
        rows = (
            await session.execute(
                select(LibraryPaper.id, LibraryPaper.paper_id)
                .join(Paper, Paper.id == LibraryPaper.paper_id)
                .where(
                    LibraryPaper.library_id == library.id,
                    LibraryPaper.status == "candidate",
                )
                .order_by(Paper.published_at.desc().nulls_last(), LibraryPaper.created_at)
                # 最大化模式打分不截断（全部 candidate 逐篇打分，哨兵防失控）
                .limit(_UNLIMITED_SENTINEL if _unlimited(knobs) else _MAX_CANDIDATES_CAP)
            )
        ).all()
        membership_ids = [mid for mid, _ in rows]
        paper_ids = [pid for _, pid in rows]

    guidance = ctx.skill_guidance("wiki.score_relevance")
    total = len(membership_ids)
    progress = {"n": 0}
    project_id = ctx.run.project_id

    async def score_one(membership_id: uuid.UUID) -> dict[str, Any] | None:
        """单篇打分：独立 session 重新加载 → 共享打分服务写字段 → 状态转移 → 自行 commit。"""
        async with get_sessionmaker()() as session:
            membership = await session.get(LibraryPaper, membership_id)
            if membership is None or membership.status != "candidate":
                return None  # 竞态/已处理：幂等跳过（正常流不会命中）
            paper = await session.get(Paper, membership.paper_id)
            if paper is None:
                return None
            progress["n"] += 1
            await ctx.log(f"相关性打分 {progress['n']}/{total}：{paper.title[:60]}")
            scored = await score_paper_relevance(
                paper,
                membership,
                context_text=context_text,
                llm=ctx.llm,
                extra_guidance=guidance,
                user_id=billing_user_id,
                project_id=project_id,
                voyage_id=ctx.run.id,
            )
            score = scored.score
            passed = score >= threshold
            if passed:
                membership.status = "scored"
                membership.trash_reason = None
            else:
                # 相关性不够的直接删掉成员行，不进回收站——自动淘汰每天上百篇，堆在
                # 回收站里没人会去看，还把「用户手动删的」淹没掉。论文本体留在内容池
                # （每日池仍引用着它），孤儿清理会在它彻底没人引用时回收。
                await delete_membership_hard(session, membership)
            # 逐篇 commit：worker 中途被杀后按 status 断点续跑，不重复打分
            await session.commit()
            return {
                "id": str(paper.id),
                "title": paper.title,
                "score": score,
                "passed": passed,
            }

    results = await _gather_bounded(_LLM_CONCURRENCY, [score_one(mid) for mid in membership_ids])
    _reraise_if_cancelled(results)  # worker 被杀须上抛，不当作单篇失败

    # gather 后统一汇总（顺序按查询顺序，稳定；不在并发任务里改 ctx.checkpoint）
    succeeded = 0
    excluded = 0
    failed: list[dict[str, str]] = []
    scored_ids: list[str] = []
    rejected_ids: list[str] = []
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
            rejected_ids.append(result["id"])
        if len(scored_brief) < _OBS_LIST_CAP:
            scored_brief.append(result)

    # 成员行删掉之后，去重集合就挡不住它们了：而 since_last 的扫描窗口和上次同步那天
    # 是重叠的，不记一笔的话今天淘汰的几百篇明天会再花一次 LLM。所以在库上留一份
    # 「判过且没通过」的名单（只存 id，有上限，够覆盖重叠窗口即可）。
    if rejected_ids:
        async with get_sessionmaker()() as session:
            library = await _resolve_library(session, ctx)
            if library is not None:
                remember_rejected(library, rejected_ids)
                await session.commit()

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
        library = await _resolve_library(session, ctx)
        billing_user_id = _ingest_billing_owner(library)
        affil_mode = await get_affiliation_extraction_mode(session)
        pairs = (
            await session.execute(
                select(Paper, LibraryPaper)
                .join(LibraryPaper, LibraryPaper.paper_id == Paper.id)
                .where(LibraryPaper.library_id == library.id, LibraryPaper.status == "scored")
                .order_by(LibraryPaper.relevance_score.desc().nulls_last())
                .limit(top_n)
            )
        ).all()
        papers = [p for p, _ in pairs]
        membership_of = {p.id: m for p, m in pairs}
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
            # 发表机构补充（高级检索用）：on_add 模式下优先 LLM 从全文标题页逐位作者解析
            # 机构（extract 内部已带失败兜底，返回 None 不写入）；on_compile 模式跳过该
            # 专门调用（改折叠进 wiki.compile）。失败不影响主流程
            if affil_mode == "on_add" and not paper.affiliations and paper.full_text_path:
                mapping = await extract_author_affiliations_llm(
                    paper,
                    llm=ctx.llm,
                    user_id=billing_user_id,
                    project_id=ctx.run.project_id,
                    library_id=library.id,
                    voyage_id=ctx.run.id,
                )
                apply_author_affiliations(paper, mapping)
            # OpenAlex 反查兜底（无全文 / LLM 未给机构 / on_compile 模式）：OpenAlex 的
            # authors 已带机构，用 apply_author_affiliations 逐位对齐并汇总 paper.affiliations。
            # DOI 论文发表日期缺失时也要反查一次（走不了上面的 arXiv 回填）。
            need_date = paper.published_at is None and not paper.arxiv_id
            if (not paper.affiliations or need_date) and (paper.arxiv_id or paper.doi):
                try:
                    meta = (
                        await get_openalex_client().get_by_arxiv(paper.arxiv_id)
                        if paper.arxiv_id
                        else await get_openalex_client().get_by_doi(paper.doi)
                    )
                    if not paper.affiliations:
                        apply_author_affiliations(paper, (meta or {}).get("authors"))
                    if paper.published_at is None:
                        paper.published_at = _parse_iso((meta or {}).get("published"))
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 — 机构补充尽力而为
                    logger.warning("affiliation enrich failed for %s", paper.id, exc_info=True)
            # 分段索引（文献问答/idea 生成的知识底座）；失败不影响主流程。
            # 有全文按全文切、没全文建「标题 + 摘要」兜底块；内容池命中的论文可能已有
            # 全文分段（其他方向建的），有则直接复用不重建
            try:
                await ensure_paper_chunks(session, paper)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                failed.append({"id": str(paper.id), "error": f"chunks: {type(e).__name__}: {e}"})
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
            membership_of[paper.id].status = "fetched"  # 无全文也进入编译（退化用摘要）
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
        library = await _resolve_library(session, ctx)
        if library is None:
            raise ValueError(f"library not found for run: {ctx.run.id}")
        billing_user_id = _ingest_billing_owner(library)
        # 幂等断点：已 compiled 的不再进入（status=fetched 才编译）。外层只查 id，每篇
        # 编译在各自独立 session 内重新加载，避免多任务共享一个 AsyncSession。
        rows = (
            await session.execute(
                select(LibraryPaper.id, LibraryPaper.paper_id)
                .where(LibraryPaper.library_id == library.id, LibraryPaper.status == "fetched")
                .order_by(LibraryPaper.relevance_score.desc().nulls_last())
                .limit(top_n)
            )
        ).all()
        membership_ids = [mid for mid, _ in rows]
        paper_ids = [pid for _, pid in rows]

    guidance = ctx.skill_guidance("wiki.compile")
    total = len(membership_ids)
    progress = {"n": 0}
    project_id = ctx.run.project_id

    async def compile_one(membership_id: uuid.UUID) -> dict[str, Any] | None:
        """单篇编译：独立 session 重新加载 → 挑图注释 → 图文编译 → 自行 commit。"""
        async with get_sessionmaker()() as session:
            membership = await session.get(LibraryPaper, membership_id)
            if membership is None or membership.status != "fetched":
                return None  # 竞态/已处理：幂等跳过（正常流不会命中）
            paper = await session.get(Paper, membership.paper_id)
            if paper is None:
                return None
            affil_mode = await get_affiliation_extraction_mode(session)
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
                        user_id=billing_user_id,
                        project_id=project_id,
                        library_id=membership.library_id,
                        voyage_id=ctx.run.id,
                    )
                    await session.commit()
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001
                    logger.warning("figure annotation failed for paper %s", paper.id, exc_info=True)
            # ② 图文编译（重要图 ≤4 张随 prompt 送入）+ ③ 无效 ![[fig:N]] 标记剥除。
            #    on_compile 模式且尚无机构时顺带带回作者↔机构映射（省一次专门调用）。
            collect_affs = affil_mode == "on_compile" and not paper.affiliations
            compiled = await compile_paper(
                paper,
                llm=ctx.llm,
                user_id=billing_user_id,
                project_id=project_id,
                library_id=membership.library_id,
                voyage_id=ctx.run.id,
                extra_guidance=guidance,
                collect_affiliations=collect_affs,
            )
            # 解读全平台一份：写 paper_wikis（已有则覆盖成本次结果）。编译者记发起
            # 本次同步的人（公共库的账记系统，billing_user_id 为空，但人是有的）
            await upsert_wiki(
                session,
                paper=paper,
                content=compiled.content,
                model=compiled.model or None,
                compiled_by=ctx.run.created_by,
            )
            membership.status = "compiled"
            if compiled.author_affiliations and not paper.affiliations:
                apply_author_affiliations(paper, compiled.author_affiliations)
            await session.commit()
            await ctx.log(
                f"✓ 完成 {progress['n']}/{total}：{paper.title[:50]}（{len(compiled.content)} 字）",
                level="success",
            )
            return {"id": str(paper.id), "title": paper.title}

    results = await _gather_bounded(_LLM_CONCURRENCY, [compile_one(mid) for mid in membership_ids])
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
        library = await _resolve_library(session, ctx)
        if library is None:
            raise ValueError(f"library not found for run: {ctx.run.id}")
        billing_user_id = _ingest_billing_owner(library)
        # 全库上链逻辑与手动补建端点共用（services/concepts.py）
        stats, papers = await link_all_paper_concepts(
            session,
            library_id=library.id,
            llm=ctx.llm,
            user_id=billing_user_id,
            project_id=ctx.run.project_id,
            voyage_id=ctx.run.id,
        )
        created = int(stats["concepts_created"])
        promoted_names = list(stats["promoted_concepts"])
        new_links = int(stats["links_created"])

        # embedding：编译完成且在激活空间下尚无向量的论文批量嵌入（不支持则跳过）
        embedded = 0
        embed_error: str | None = None
        space = await active_space(session)
        done = (
            await papers_with_vector(session, [p.id for p in papers], space)
            if space is not None
            else set()
        )
        pending = [p for p in papers if p.id not in done]
        if pending:
            # 文本口径与手动补全/每日池共用（services/paper_enrich.paper_embedding_text）
            texts = [paper_embedding_text(p) for p in pending]
            try:
                vectors, batch_space = await embed_documents(
                    session,
                    texts,
                    user_id=billing_user_id,
                    project_id=ctx.run.project_id,
                    library_id=library.id,
                    voyage_id=ctx.run.id,
                )
                for paper, vector in zip(pending, vectors, strict=True):
                    await upsert_paper_vector(session, paper.id, vector, batch_space)
                    embedded += 1
                await session.commit()
            except asyncio.CancelledError:
                raise
            except NotImplementedError:
                embed_error = "provider does not support embeddings"
            except Exception as e:  # noqa: BLE001 — 嵌入失败不影响上链结果
                embed_error = f"{type(e).__name__}: {e}"

        # 摘要兜底块的向量 = 论文级向量的拷贝（零 token，不受任何开关控制），排在嵌入之后
        await sync_abstract_chunk_vectors(session, paper_ids=[p.id for p in papers])
        await session.commit()

        # 分段向量：补齐缺失的 chunk embedding（文献问答检索底座）。
        # 最大化模式放开单次 2000 段的默认上限（否则大批量编译后向量长期欠账）
        embed_kwargs: dict[str, Any] = (
            {"limit": _UNLIMITED_CHUNK_SENTINEL} if _unlimited(_knobs(ctx)) else {}
        )
        chunks_embedded, chunk_embed_error = await embed_pending_chunks(
            session,
            library_id=library.id,
            llm=ctx.llm,
            user_id=billing_user_id,
            project_id=ctx.run.project_id,
            voyage_id=ctx.run.id,
            **embed_kwargs,
        )

    return {
        "papers": len(papers),
        "concepts_created": created,
        "links_created": new_links,
        "links_removed": int(stats["links_removed"]),
        "concepts_removed": int(stats["concepts_removed"]),
        # 新词条先记候选（不可见、不生成定义），被 ≥2 篇论文用到才复核转正进概念库；
        # 面向用户的「新增概念」报的是转正数，不是 concepts_created
        "concepts_promoted": int(stats["concepts_promoted"]),
        "new_concepts": promoted_names[:_OBS_LIST_CAP],
        # 复核时被判定「根本不是概念」而下架的（fig:1、编号、半句话……）
        "concepts_rejected": int(stats["concepts_rejected"]),
        "embedded": embedded,
        "embed_error": embed_error,
        "chunks_embedded": chunks_embedded,
        "chunk_embed_error": chunk_embed_error,
    }


# ---- 7. 更新水位线 + 活动流 ----


@register("wiki.update_watermark")
async def update_watermark(ctx: ActionContext, params: dict[str, Any]) -> dict[str, Any]:
    async with get_sessionmaker()() as session:
        library = await _resolve_library(session, ctx)
        # 水位线权威源在库（P8a）：写 library.ingest_state；起源课题 project.ingest_state
        # 不再是权威（overview「上次同步时间」读库）。
        state = dict((library.ingest_state if library else None) or {})
        previous = state.get("watermark")

        # 预算耗尽时引擎会把剩余步骤置 obsolete，再拿最后一个待办步骤当隐式收尾——
        # 而本流水线的最后一步正是这里。那种情况下推进水位线等于「一篇没处理，却宣布
        # 这段时间已经看过了」，下次同步会直接跳过这批论文。所以截断时保持原水位线。
        truncated = (
            await session.scalar(
                select(func.count())
                .select_from(VoyageStep)
                .where(VoyageStep.run_id == ctx.run.id, VoyageStep.status == "obsolete")
            )
        ) or 0
        watermark = (
            previous if truncated else (ctx.checkpoint.get("watermark_candidate") or previous)
        )
        finished_at = utcnow().isoformat()
        state["watermark"] = watermark
        state["last_run"] = {"voyage_id": str(ctx.run.id), "finished_at": finished_at}
        if library is not None:
            library.ingest_state = state

        compiled_count = int(ctx.checkpoint.get("compiled_count") or 0)
        message = (
            f"文献调研被预算截断：本次编译 {compiled_count} 篇；进度未记录，下次会重新处理"
            if truncated
            else f"文献调研完成：本次编译 {compiled_count} 篇 wiki 页"
        )
        session.add(
            Activity(
                project_id=ctx.run.project_id,
                library_id=library.id if library is not None else None,
                actor="agent:librarian",
                kind="ingest.completed",
                message=message,
                payload={
                    "voyage_id": str(ctx.run.id),
                    "mode": _mode(ctx),
                    "compiled": compiled_count,
                    "watermark": watermark,
                    "truncated": bool(truncated),
                },
            )
        )
        await session.commit()

    return {
        "watermark": watermark,
        "compiled": int(ctx.checkpoint.get("compiled_count") or 0),
        "truncated": bool(truncated),
    }
