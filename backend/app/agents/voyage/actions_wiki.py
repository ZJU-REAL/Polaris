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
import json
import logging
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.voyage.actions import ActionContext, register
from app.core.db import get_sessionmaker
from app.core.llm.base import Message
from app.models.activity import Activity
from app.models.base import utcnow
from app.models.paper import Concept, Paper, paper_concepts
from app.models.project import Project
from app.services.concepts import extract_wikilinks, normalize_category, wiki_slug
from app.services.figure_annotate import annotate_figures, figures_annotated
from app.services.literature import get_arxiv_client, get_s2_client
from app.services.literature.arxiv import normalize_arxiv_id
from app.services.literature.pdf_extract import extract_figures, extract_full_text, save_pdf
from app.services.projects import DEFAULT_ARXIV_CATEGORIES
from app.services.wiki_compile import compile_paper

logger = logging.getLogger(__name__)

DEFAULT_KNOBS: dict[str, Any] = {
    "months_back": 6,
    "max_papers": 50,
    "relevance_threshold": 0.6,
    "snowball_depth": 1,
    "compile_top_n": 20,
}

_MAX_CANDIDATES_CAP = 200

RELEVANCE_SYSTEM_PROMPT = """\
你是文献相关性评审，对照研究方向定义评估一篇论文（只看标题与摘要）。
只输出一个 JSON 对象，不要输出任何其他文字或 Markdown 代码块，格式：
{"score": 0 到 1 之间的小数, "reason": "简要理由", "tldr": "一句话中文总结"}
"""

CONCEPT_DEF_SYSTEM_PROMPT = """\
你是 Librarian，为研究 wiki 的新概念词条给出一句话中文定义与类别。
只输出一个 JSON 对象，不要输出任何其他文字，格式：
{"concepts": [{"name": "概念名", "definition": "一句话定义", \
"category": "method|architecture|methodology|problem|metric|dataset|other"}]}
"""


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


def _extract_json(content: str) -> Any:
    start = content.find("{")
    end = content.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object found")
    return json.loads(content[start : end + 1])


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


@register("wiki.search_candidates")
async def search_candidates(ctx: ActionContext, params: dict[str, Any]) -> dict[str, Any]:
    knobs = _knobs(ctx)
    now = utcnow()
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
        if _mode(ctx) == "incremental" and watermark is not None:
            since = watermark - timedelta(days=1)  # 1 天重叠窗口，防边界漏抓
        else:
            since = now - timedelta(days=30 * int(knobs["months_back"]))

        limit = min(_MAX_CANDIDATES_CAP, max(int(knobs["max_papers"]) * 3, 10))
        entries = await get_arxiv_client().search(
            categories=categories, keywords=include, since=since, until=now, limit=limit
        )

        arxiv_ids, dois, titles = await _existing_keys(session, project.id)
        inserted = 0
        for entry in entries:
            aid = entry.get("arxiv_id")
            title = (entry.get("title") or "").strip()
            if not title or (aid and aid in arxiv_ids) or title.lower() in titles:
                continue
            session.add(
                Paper(
                    project_id=project.id,
                    source="arxiv",
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
                    status="candidate",
                )
            )
            if aid:
                arxiv_ids.add(aid)
            titles.add(title.lower())
            inserted += 1
        await session.commit()

    # 新水位线 = 本次检索时刻，由 wiki.update_watermark 落库
    ctx.checkpoint["watermark_candidate"] = now.isoformat()
    return {
        "found": len(entries),
        "inserted": inserted,
        "window_since": since.isoformat(),
        "mode": _mode(ctx),
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
    inserted = 0
    max_new = int(knobs["max_papers"]) * 2

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
                    session.add(
                        Paper(
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
                            status="candidate",
                        )
                    )
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
        await session.commit()

    return {
        "depth": depth,
        "processed": processed_seeds,
        "inserted": inserted,
        "failed": failed,
    }


# ---- 3. 相关性打分（LLM stage=relevance） ----


@register("wiki.score_relevance")
async def score_relevance(ctx: ActionContext, params: dict[str, Any]) -> dict[str, Any]:
    knobs = _knobs(ctx)
    threshold = float(knobs["relevance_threshold"])
    succeeded = 0
    excluded = 0
    failed: list[dict[str, str]] = []
    scored_ids: list[str] = []

    async with get_sessionmaker()() as session:
        project = await _get_project(session, ctx)
        definition = _definition(project)
        # 稀疏 definition 容忍：rubric / questions 缺失时 prompt 只用 statement
        rubric = definition.get("rubric") or []
        questions = definition.get("questions") or []
        statement = definition.get("statement") or project.name
        context_lines = [f"研究方向：{statement}"]
        if rubric:
            context_lines.append(f"评分标准（rubric）：{json.dumps(rubric, ensure_ascii=False)}")
        if questions:
            context_lines.append(f"研究问题：{json.dumps(questions, ensure_ascii=False)}")
        context_text = "\n".join(context_lines)

        # 幂等断点：只取仍是 candidate 的论文（已打分的不重复调 LLM）
        papers = (
            (
                await session.execute(
                    select(Paper)
                    .where(Paper.project_id == project.id, Paper.status == "candidate")
                    .order_by(Paper.published_at.desc().nulls_last(), Paper.created_at)
                    .limit(_MAX_CANDIDATES_CAP)
                )
            )
            .scalars()
            .all()
        )

        for paper in papers:
            user_prompt = (
                f"{context_text}\n标题：{paper.title}\n摘要：{paper.abstract or '（无摘要）'}"
            )
            try:
                result = await ctx.llm.complete(
                    "relevance",
                    [
                        Message(role="system", content=RELEVANCE_SYSTEM_PROMPT),
                        Message(role="user", content=user_prompt),
                    ],
                    user_id=ctx.run.created_by,
                    project_id=ctx.run.project_id,
                    voyage_id=ctx.run.id,
                )
                data = _extract_json(result.content)
                score = min(1.0, max(0.0, float(data["score"])))
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 — 单篇失败跳过
                failed.append({"id": str(paper.id), "error": f"{type(e).__name__}: {e}"})
                continue
            paper.relevance_score = score
            paper.tldr = str(data.get("tldr") or "") or paper.tldr
            paper.scored_at = utcnow()
            paper.status = "scored" if score >= threshold else "excluded"
            if paper.status == "excluded":
                excluded += 1
            # 逐篇 commit：worker 中途被杀后按 status 断点续跑，不重复打分
            await session.commit()
            succeeded += 1
            scored_ids.append(str(paper.id))

    # 步内进度记入 checkpoint（审计用；幂等本身靠 Paper.status）
    ctx.checkpoint["scored_ids"] = list(ctx.checkpoint.get("scored_ids") or []) + scored_ids
    return {
        "processed": len(scored_ids) + len(failed),
        "succeeded": succeeded,
        "excluded": excluded,
        "threshold": threshold,
        "failed": failed,
    }


# ---- 4. 下载 PDF + 抽全文（PyMuPDF，失败降级 abstract） ----


def _compile_limit(knobs: dict[str, Any]) -> int:
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

    return {"processed": len(papers), "succeeded": fetched, "degraded": degraded, "failed": failed}


# ---- 5. Librarian 图文编译（LLM stage=librarian 多模态，全文优先，中文 wiki 页） ----


@register("wiki.compile")
async def compile_wiki(ctx: ActionContext, params: dict[str, Any]) -> dict[str, Any]:
    knobs = _knobs(ctx)
    top_n = _compile_limit(knobs)
    compiled = 0
    failed: list[dict[str, str]] = []

    async with get_sessionmaker()() as session:
        project = await _get_project(session, ctx)
        statement = _definition(project).get("statement") or project.name
        # 幂等断点：已 compiled 的不再进入（status=fetched 才编译）
        papers = (
            (
                await session.execute(
                    select(Paper)
                    .where(Paper.project_id == project.id, Paper.status == "fetched")
                    .order_by(Paper.relevance_score.desc().nulls_last())
                    .limit(top_n)
                )
            )
            .scalars()
            .all()
        )
        for paper in papers:
            # ① 编译前筛选注释论文图（stage=librarian 多模态）：图文编译要用重要图；
            #    失败仅 log（annotate 内部已带降级），不影响编译
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
            try:
                content = await compile_paper(
                    paper,
                    statement=statement,
                    llm=ctx.llm,
                    user_id=ctx.run.created_by,
                    voyage_id=ctx.run.id,
                )
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 — 单篇失败跳过，下次续跑重试
                failed.append({"id": str(paper.id), "error": f"{type(e).__name__}: {e}"})
                continue
            paper.wiki_content = content
            paper.compiled_at = utcnow()
            paper.status = "compiled"
            await session.commit()
            compiled += 1

    ctx.checkpoint["compiled_count"] = int(ctx.checkpoint.get("compiled_count") or 0) + compiled
    return {"processed": len(papers), "succeeded": compiled, "failed": failed}


# ---- 6. 概念上链 + embedding ----


@register("wiki.link_concepts")
async def link_concepts(ctx: ActionContext, params: dict[str, Any]) -> dict[str, Any]:
    async with get_sessionmaker()() as session:
        project = await _get_project(session, ctx)
        papers = (
            (
                await session.execute(
                    select(Paper).where(
                        Paper.project_id == project.id,
                        Paper.status.in_(("compiled", "included")),
                        Paper.wiki_content.is_not(None),
                    )
                )
            )
            .scalars()
            .all()
        )
        links_by_paper = {p.id: extract_wikilinks(p.wiki_content or "") for p in papers}
        all_names = sorted({name for names in links_by_paper.values() for name in names})

        existing = (
            (await session.execute(select(Concept).where(Concept.project_id == project.id)))
            .scalars()
            .all()
        )
        by_name = {c.name: c for c in existing}
        slugs = {c.slug for c in existing}
        new_names = [n for n in all_names if n not in by_name]

        # 新概念一次批量调 LLM 拿定义与类别；失败则用占位定义（确定性兜底）
        definitions: dict[str, dict[str, str]] = {}
        if new_names:
            try:
                result = await ctx.llm.complete(
                    "librarian",
                    [
                        Message(role="system", content=CONCEPT_DEF_SYSTEM_PROMPT),
                        Message(
                            role="user",
                            content="概念列表：" + json.dumps(new_names, ensure_ascii=False),
                        ),
                    ],
                    user_id=ctx.run.created_by,
                    project_id=ctx.run.project_id,
                    voyage_id=ctx.run.id,
                )
                for item in _extract_json(result.content).get("concepts", []):
                    if isinstance(item, dict) and item.get("name"):
                        definitions[str(item["name"])] = item
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                definitions = {}

        created = 0
        for name in new_names:
            slug = wiki_slug(name)
            if slug in slugs:
                slug = f"{slug}-{uuid.uuid4().hex[:6]}"
            slugs.add(slug)
            meta = definitions.get(name) or {}
            concept = Concept(
                project_id=project.id,
                name=name,
                slug=slug,
                definition=str(meta.get("definition") or f"{name}（定义待补充）"),
                category=normalize_category(meta.get("category")),
            )
            session.add(concept)
            by_name[name] = concept
            created += 1
        await session.flush()

        # 上链（paper_concepts），跳过已存在的关联
        existing_pairs = {
            (pid, cid)
            for pid, cid in (
                await session.execute(
                    select(paper_concepts.c.paper_id, paper_concepts.c.concept_id).where(
                        paper_concepts.c.paper_id.in_(list(links_by_paper))
                    )
                )
            ).all()
        }
        new_links = 0
        for paper_id, names in links_by_paper.items():
            for name in names:
                concept = by_name.get(name)
                if concept is None or (paper_id, concept.id) in existing_pairs:
                    continue
                await session.execute(
                    insert(paper_concepts).values(paper_id=paper_id, concept_id=concept.id)
                )
                existing_pairs.add((paper_id, concept.id))
                new_links += 1
        await session.commit()

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

    return {
        "papers": len(papers),
        "concepts_created": created,
        "links_created": new_links,
        "embedded": embedded,
        "embed_error": embed_error,
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
