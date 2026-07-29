"""Import an auto-idea Obsidian vault into the Polaris literature pool.

The importer is deliberately one-way and idempotent: the vault is read-only, papers are
deduplicated by arXiv id, and existing Polaris content is preserved unless the caller opts in
to overwriting imported wiki pages.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.base import utcnow
from app.models.library_direction import DirectionLibrary, LibraryPaper, TopicSourceLibrary
from app.models.paper import Concept, Paper, PaperChunk, PaperWiki, new_paper, paper_concepts
from app.models.project import Project
from app.models.research_digest import LibraryResearchDigest
from app.services.chunks import ensure_paper_chunks
from app.services.dedup import pool_dedup_key
from app.services.libraries import create_library, get_source_library_ids
from app.services.literature.arxiv import normalize_arxiv_id
from app.services.literature.pdf_extract import figure_path, papers_dir

logger = logging.getLogger(__name__)

_FRONTMATTER_RE = re.compile(r"\A---\s*\n.*?\n---\s*(?:\n|\Z)", re.DOTALL)
_CONCEPT_LINK_RE = re.compile(r"\[\[concepts/([^\]|]+)(?:\|([^\]]+))?\]\]")
_PAPER_LINK_RE = re.compile(r"\[\[(?:papers|surveys)/([^\]|]+)(?:\|([^\]]+))?\]\]")
_VAULT_FIGURE_RE = re.compile(r"!\[[^\]]*\]\(\.\./\.\./raw/figures/[^)]+\.png\)")
_LOCAL_SOURCE_FOOTER_RE = re.compile(r"\n---\s*\n本地原文：.*\Z", re.DOTALL)
_TLDR_RE = re.compile(r"^## TL;DR\s*\n(.+?)(?=\n##\s|\Z)", re.MULTILINE | re.DOTALL)


@dataclass(slots=True)
class VaultPaper:
    slug: str
    arxiv_id: str
    title: str
    abstract: str | None
    authors: list[str]
    categories: list[str]
    published_at: datetime | None
    relevance_score: float | None
    pdf_path: Path | None
    text_path: Path | None
    wiki_path: Path | None
    wiki_raw: str | None
    figure_path: Path | None
    figure_meta: dict[str, Any] | None
    concept_slugs: set[str] = field(default_factory=set)


@dataclass(slots=True)
class VaultConcept:
    slug: str
    name: str
    definition: str | None
    wiki_raw: str


@dataclass(slots=True)
class VaultReport:
    report_date: date
    wiki_raw: str


@dataclass(slots=True)
class VaultSnapshot:
    root: Path
    papers: list[VaultPaper]
    concepts: dict[str, VaultConcept]
    statement: str | None
    paper_by_slug: dict[str, VaultPaper]
    reports: list[VaultReport]
    trends_raw: str | None


@dataclass(slots=True)
class SyncStats:
    project_id: str
    library_id: str | None = None
    library_created: bool = False
    scanned_papers: int = 0
    missing_wikis: int = 0
    new_papers: int = 0
    reused_papers: int = 0
    memberships_created: int = 0
    wikis_created: int = 0
    wikis_overwritten: int = 0
    wikis_preserved: int = 0
    pdfs_copied: int = 0
    texts_copied: int = 0
    figures_copied: int = 0
    concepts_created: int = 0
    concepts_reused: int = 0
    concept_links_created: int = 0
    papers_indexed: int = 0
    chunks_created: int = 0
    reports_created: int = 0
    reports_updated: int = 0

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _read_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"expected a JSON object: {path}")
    return data


def strip_frontmatter(markdown: str) -> str:
    return _FRONTMATTER_RE.sub("", markdown, count=1).strip()


def _parse_date(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _first_heading(markdown: str, fallback: str) -> str:
    body = strip_frontmatter(markdown)
    match = re.search(r"^#\s+(.+?)\s*$", body, re.MULTILINE)
    return match.group(1).strip() if match else fallback


def _concept_definition(markdown: str) -> str | None:
    body = strip_frontmatter(markdown)
    match = re.search(r"^\*\*定义\*\*[:：]\s*(.+?)\s*$", body, re.MULTILINE)
    return match.group(1).strip() if match else None


def _statement(root: Path) -> str | None:
    path = root / "research_direction.md"
    if not path.is_file():
        return None
    text = path.read_text(encoding="utf-8")
    match = re.search(r"^## 一句话\s*\n(.+?)(?=\n##\s|\Z)", text, re.MULTILINE | re.DOTALL)
    return match.group(1).strip() if match else None


def scan_vault(root: Path) -> VaultSnapshot:
    """Validate and load the stable subset of an auto-idea Obsidian vault."""
    root = root.expanduser().resolve()
    meta_dir = root / "raw" / "meta"
    if not meta_dir.is_dir():
        raise ValueError(f"not an auto-idea vault (missing raw/meta): {root}")

    concepts: dict[str, VaultConcept] = {}
    concept_dir = root / "wiki" / "concepts"
    if concept_dir.is_dir():
        for path in sorted(concept_dir.glob("*.md")):
            raw = path.read_text(encoding="utf-8")
            concepts[path.stem] = VaultConcept(
                slug=path.stem,
                name=_first_heading(raw, path.stem.replace("-", " ")),
                definition=_concept_definition(raw),
                wiki_raw=raw,
            )

    reports: list[VaultReport] = []
    report_dir = root / "report"
    if report_dir.is_dir():
        for path in sorted(report_dir.glob("*.md")):
            try:
                report_date = date.fromisoformat(path.stem)
            except ValueError:
                logger.warning("skip non-date vault report: %s", path)
                continue
            reports.append(
                VaultReport(
                    report_date=report_date,
                    wiki_raw=path.read_text(encoding="utf-8"),
                )
            )
    trends_path = root / "wiki" / "trends.md"
    trends_raw = trends_path.read_text(encoding="utf-8") if trends_path.is_file() else None

    papers: list[VaultPaper] = []
    seen_arxiv: set[str] = set()
    for meta_path in sorted(meta_dir.glob("*.json")):
        meta = _read_json(meta_path)
        slug = str(meta.get("slug") or meta_path.stem).strip()
        raw_arxiv = str(meta.get("arxiv_id") or "").strip()
        title = str(meta.get("title") or "").strip()
        if not raw_arxiv or not title:
            raise ValueError(f"paper needs arxiv_id and title: {meta_path}")
        arxiv_id = normalize_arxiv_id(raw_arxiv)
        if arxiv_id in seen_arxiv:
            raise ValueError(f"duplicate arXiv id {arxiv_id} in {meta_path}")
        seen_arxiv.add(arxiv_id)

        wiki_candidates = [
            root / "wiki" / "papers" / f"{slug}.md",
            root / "wiki" / "surveys" / f"{slug}.md",
        ]
        wiki_path = next((p for p in wiki_candidates if p.is_file()), None)
        wiki_raw = wiki_path.read_text(encoding="utf-8") if wiki_path else None
        concept_slugs = set(_CONCEPT_LINK_RE.findall(wiki_raw or ""))
        concept_slugs = {slug_value for slug_value, _label in concept_slugs}

        pdf_candidate = root / "raw" / "paper" / f"{slug}.pdf"
        text_candidate = root / "raw" / "text" / f"{slug}.txt"
        figure_candidate = root / "raw" / "figures" / f"{slug}.png"
        figure_meta_path = root / "raw" / "figures" / f"{slug}.json"
        authors = meta.get("authors") if isinstance(meta.get("authors"), list) else []
        categories = meta.get("categories") if isinstance(meta.get("categories"), list) else []
        papers.append(
            VaultPaper(
                slug=slug,
                arxiv_id=arxiv_id,
                title=title,
                abstract=(str(meta["abstract"]).strip() if meta.get("abstract") else None),
                authors=[str(author).strip() for author in authors if str(author).strip()],
                categories=[
                    str(category).strip() for category in categories if str(category).strip()
                ],
                published_at=_parse_date(meta.get("published")),
                relevance_score=_float_or_none(meta.get("relevance_score")),
                pdf_path=pdf_candidate if pdf_candidate.is_file() else None,
                text_path=text_candidate if text_candidate.is_file() else None,
                wiki_path=wiki_path,
                wiki_raw=wiki_raw,
                figure_path=figure_candidate if figure_candidate.is_file() else None,
                figure_meta=_read_json(figure_meta_path) if figure_meta_path.is_file() else None,
                concept_slugs=concept_slugs,
            )
        )
    if not papers:
        raise ValueError(f"vault contains no paper metadata: {root}")
    return VaultSnapshot(
        root=root,
        papers=papers,
        concepts=concepts,
        statement=_statement(root),
        paper_by_slug={paper.slug: paper for paper in papers},
        reports=reports,
        trends_raw=trends_raw,
    )


def render_markdown(
    markdown: str,
    *,
    snapshot: VaultSnapshot,
    figure_index: int | None = None,
    paper_ids_by_slug: dict[str, uuid.UUID] | None = None,
) -> str:
    """Rewrite vault-local links into forms understood by the Polaris markdown renderer."""
    body = strip_frontmatter(markdown)
    body = _LOCAL_SOURCE_FOOTER_RE.sub("", body).rstrip()

    def concept_repl(match: re.Match[str]) -> str:
        slug, label = match.group(1), match.group(2)
        concept = snapshot.concepts.get(slug)
        return f"[[{label or (concept.name if concept else slug.replace('-', ' '))}]]"

    def paper_repl(match: re.Match[str]) -> str:
        slug, label = match.group(1), match.group(2)
        paper = snapshot.paper_by_slug.get(slug)
        if paper is None:
            return label or slug.replace("-", " ")
        paper_id = (paper_ids_by_slug or {}).get(slug)
        if paper_id is not None:
            return f"[[paper:{paper_id}|{label or paper.title}]]"
        return f"[{label or paper.title}](https://arxiv.org/abs/{paper.arxiv_id})"

    body = _CONCEPT_LINK_RE.sub(concept_repl, body)
    body = _PAPER_LINK_RE.sub(paper_repl, body)
    if figure_index is None:
        body = _VAULT_FIGURE_RE.sub("", body)
    else:
        body = _VAULT_FIGURE_RE.sub(f"![[fig:{figure_index}]]", body)
    return body.strip()


def extract_tldr(markdown: str | None) -> str | None:
    if not markdown:
        return None
    match = _TLDR_RE.search(strip_frontmatter(markdown))
    if not match:
        return None
    return " ".join(match.group(1).strip().split())


def _atomic_copy(source: Path, destination: Path) -> bool:
    """Copy only when missing, using a same-directory temporary file for atomic replacement."""
    if destination.is_file():
        return False
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    try:
        shutil.copy2(source, temporary)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return True


def _obsidian_figure_index(paper: Paper, slug: str) -> int | None:
    for item in paper.figures or []:
        if item.get("source") == "obsidian-vault" and item.get("vault_slug") == slug:
            return int(item["index"])
    return None


async def _resolve_library(
    session: AsyncSession,
    *,
    project: Project,
    name: str,
    statement: str | None,
    dry_run: bool,
) -> tuple[DirectionLibrary | None, bool]:
    source_ids = await get_source_library_ids(session, project.id)
    if source_ids:
        linked = list(
            (
                await session.execute(
                    select(DirectionLibrary).where(DirectionLibrary.id.in_(source_ids))
                )
            )
            .scalars()
            .all()
        )
        exact = next((library for library in linked if library.name == name), None)
        if exact is not None:
            return exact, False
    if dry_run:
        return None, True
    library = await create_library(
        session,
        name=name,
        statement=statement,
        created_by=project.owner_id,
        status="active",
    )
    session.add(TopicSourceLibrary(topic_id=project.id, library_id=library.id))
    await session.commit()
    return library, True


async def _paper_pool(session: AsyncSession, snapshot: VaultSnapshot) -> dict[str, Paper]:
    ids = [paper.arxiv_id for paper in snapshot.papers]
    keys = [f"arxiv:{arxiv_id.lower()}" for arxiv_id in ids]
    rows = list(
        (
            await session.execute(
                select(Paper).where(or_(Paper.arxiv_id.in_(ids), Paper.dedup_key.in_(keys)))
            )
        )
        .scalars()
        .all()
    )
    found: dict[str, Paper] = {}
    for paper in rows:
        if paper.arxiv_id:
            found[normalize_arxiv_id(paper.arxiv_id)] = paper
        elif paper.dedup_key and paper.dedup_key.startswith("arxiv:"):
            found[paper.dedup_key.removeprefix("arxiv:")] = paper
    return found


async def _sync_concepts(
    session: AsyncSession,
    snapshot: VaultSnapshot,
    stats: SyncStats,
    *,
    dry_run: bool,
) -> dict[str, Concept]:
    slugs = list(snapshot.concepts)
    existing = {
        concept.slug: concept
        for concept in (
            (await session.execute(select(Concept).where(Concept.slug.in_(slugs)))).scalars().all()
            if slugs
            else []
        )
    }
    stats.concepts_reused = len(existing)
    stats.concepts_created = len(slugs) - len(existing)
    if dry_run:
        return existing
    for slug, source in snapshot.concepts.items():
        rendered = render_markdown(source.wiki_raw, snapshot=snapshot)
        concept = existing.get(slug)
        if concept is None:
            concept = Concept(
                slug=slug,
                name=source.name,
                definition=source.definition,
                wiki_content=rendered,
                category="other",
                status="active",
            )
            session.add(concept)
            existing[slug] = concept
        else:
            if not concept.definition and source.definition:
                concept.definition = source.definition
            if not concept.wiki_content:
                concept.wiki_content = rendered
            if concept.status == "candidate":
                concept.status = "active"
    await session.flush()
    return existing


def _fill_missing_metadata(paper: Paper, source: VaultPaper) -> None:
    if not paper.source:
        paper.source = "arxiv"
    if not paper.arxiv_id:
        paper.arxiv_id = source.arxiv_id
    if not paper.dedup_key:
        paper.dedup_key = pool_dedup_key(
            arxiv_id=source.arxiv_id,
            doi=None,
            title=source.title,
            year=source.published_at.year if source.published_at else None,
            authors=[{"name": name} for name in source.authors],
        )
    if not paper.abstract:
        paper.abstract = source.abstract
    if not paper.authors:
        paper.authors = [{"name": name} for name in source.authors]
    if not paper.published_at:
        paper.published_at = source.published_at
    if not paper.year and source.published_at:
        paper.year = source.published_at.year
    if not paper.url:
        paper.url = f"https://arxiv.org/abs/{source.arxiv_id}"
    if not paper.external_ids:
        paper.external_ids = {"ArXiv": source.arxiv_id}
    if not paper.tldr:
        paper.tldr = extract_tldr(source.wiki_raw)


def _imported_report_counts(markdown: str) -> dict[str, int]:
    """从 auto-idea 报告标题/排除清单提取最稳定的候选、入库、排除数字。"""
    counts = {
        "source_fetched": 0,
        "prescreened": 0,
        "inserted": 0,
        "kept": 0,
        "excluded": 0,
        "compiled": 0,
    }
    match = re.search(
        r"排除清单[^\n]*?([0-9]+)\s*候选\s*[−-]\s*([0-9]+)\s*入库\s*=\s*([0-9]+)",
        markdown,
    )
    if not match:
        match = re.search(r"候选\s*([0-9]+).*?(?:→|->)\s*\+?([0-9]+)", markdown)
        if match:
            source, kept = (int(value) for value in match.groups())
            excluded = max(0, source - kept)
        else:
            return counts
    else:
        source, kept, excluded = (int(value) for value in match.groups())
    counts.update(
        {
            "source_fetched": source,
            "prescreened": source,
            "inserted": kept,
            "kept": kept,
            "excluded": excluded,
            "compiled": kept,
        }
    )
    return counts


def _imported_report_summary(markdown: str, report_date: date) -> str:
    body = strip_frontmatter(markdown)
    match = re.search(r"^## 本期观察\s*\n(.+?)(?=\n##\s|\Z)", body, re.MULTILINE | re.DOTALL)
    text = match.group(1) if match else body
    text = re.sub(r"[#>*_`\[\]]", " ", text)
    text = " ".join(text.split())
    return text[:500] or f"从 Obsidian 导入的 {report_date.isoformat()} 每日简报"


def _imported_report_papers(
    markdown: str,
    *,
    snapshot: VaultSnapshot,
    paper_ids_by_slug: dict[str, uuid.UUID],
) -> list[dict[str, Any]]:
    """提取日报引用论文，供前端把内部引用显示为论文标题并跳转库内详情。"""
    insights: list[dict[str, Any]] = []
    seen: set[uuid.UUID] = set()
    for match in _PAPER_LINK_RE.finditer(markdown):
        slug = match.group(1)
        paper_id = paper_ids_by_slug.get(slug)
        paper = snapshot.paper_by_slug.get(slug)
        if paper_id is None or paper is None or paper_id in seen:
            continue
        seen.add(paper_id)
        insights.append(
            {
                "paper_id": str(paper_id),
                "title": match.group(2) or paper.title,
                "tldr": extract_tldr(paper.wiki_raw) or paper.abstract,
                "relevance_score": paper.relevance_score,
                "status": "compiled" if paper.wiki_raw else "included",
            }
        )
    return insights


async def _sync_reports(
    session: AsyncSession,
    *,
    snapshot: VaultSnapshot,
    library: DirectionLibrary,
    stats: SyncStats,
    paper_ids_by_slug: dict[str, uuid.UUID],
) -> None:
    """幂等导入历史日报；最新日报附带 vault 当前滚动趋势快照。"""
    if not snapshot.reports:
        return
    dates = [report.report_date for report in snapshot.reports]
    existing = {
        row.report_date: row
        for row in (
            (
                await session.execute(
                    select(LibraryResearchDigest).where(
                        LibraryResearchDigest.library_id == library.id,
                        LibraryResearchDigest.source == "obsidian",
                        LibraryResearchDigest.report_date.in_(dates),
                    )
                )
            )
            .scalars()
            .all()
        )
    }
    latest_date = max(dates)
    rendered_trends = (
        render_markdown(
            snapshot.trends_raw,
            snapshot=snapshot,
            paper_ids_by_slug=paper_ids_by_slug,
        )
        if snapshot.trends_raw
        else ""
    )
    for report in snapshot.reports:
        content = render_markdown(
            report.wiki_raw,
            snapshot=snapshot,
            paper_ids_by_slug=paper_ids_by_slug,
        )
        counts = _imported_report_counts(content)
        summary = _imported_report_summary(content, report.report_date)
        paper_insights = _imported_report_papers(
            report.wiki_raw,
            snapshot=snapshot,
            paper_ids_by_slug=paper_ids_by_slug,
        )
        trend_content = rendered_trends if report.report_date == latest_date else ""
        row = existing.get(report.report_date)
        if row is None:
            session.add(
                LibraryResearchDigest(
                    library_id=library.id,
                    voyage_id=None,
                    report_date=report.report_date,
                    source="obsidian",
                    mode="import",
                    counts=counts,
                    source_diagnostics={
                        "status": "imported",
                        "messages": ["历史简报由 Obsidian vault 导入。"],
                    },
                    paper_insights=paper_insights,
                    excluded_papers=[],
                    cross_paper_signals=[],
                    summary=summary,
                    content=content,
                    model="obsidian-vault-import",
                    rolling_trends=[],
                    trend_content=trend_content,
                    trend_model="obsidian-vault-import" if trend_content else None,
                )
            )
            stats.reports_created += 1
            continue
        changed = (
            row.content != content
            or row.counts != counts
            or row.summary != summary
            or row.paper_insights != paper_insights
            or row.trend_content != trend_content
        )
        if changed:
            row.counts = counts
            row.summary = summary
            row.content = content
            row.paper_insights = paper_insights
            row.trend_content = trend_content
            row.trend_model = "obsidian-vault-import" if trend_content else None
            stats.reports_updated += 1


async def sync_obsidian_vault(
    session: AsyncSession,
    *,
    vault_path: Path,
    project_id: uuid.UUID,
    library_name: str | None = None,
    dry_run: bool = False,
    overwrite_wiki: bool = False,
    copy_files: bool = True,
    index_chunks: bool = True,
    batch_size: int = 25,
) -> SyncStats:
    """Synchronize a vault into one project-linked library and return an audit summary."""
    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    snapshot = scan_vault(vault_path)
    project = await session.get(Project, project_id)
    if project is None:
        raise ValueError(f"project does not exist: {project_id}")
    stats = SyncStats(
        project_id=str(project.id),
        scanned_papers=len(snapshot.papers),
        missing_wikis=sum(paper.wiki_raw is None for paper in snapshot.papers),
    )
    library, library_created = await _resolve_library(
        session,
        project=project,
        name=library_name or project.name,
        statement=snapshot.statement or project.statement,
        dry_run=dry_run,
    )
    stats.library_created = library_created
    stats.library_id = str(library.id) if library else None

    pool = await _paper_pool(session, snapshot)
    stats.reused_papers = len(pool)
    stats.new_papers = len(snapshot.papers) - len(pool)
    concepts = await _sync_concepts(session, snapshot, stats, dry_run=dry_run)

    existing_members: set[uuid.UUID] = set()
    existing_wikis: set[uuid.UUID] = set()
    existing_chunks: set[uuid.UUID] = set()
    existing_associations: set[tuple[uuid.UUID, uuid.UUID]] = set()
    if pool:
        paper_ids = [paper.id for paper in pool.values()]
        existing_wikis = set(
            (
                await session.execute(
                    select(PaperWiki.paper_id).where(PaperWiki.paper_id.in_(paper_ids))
                )
            )
            .scalars()
            .all()
        )
        existing_chunks = set(
            (
                await session.execute(
                    select(PaperChunk.paper_id).where(PaperChunk.paper_id.in_(paper_ids)).distinct()
                )
            )
            .scalars()
            .all()
        )
        if library is not None:
            existing_members = set(
                (
                    await session.execute(
                        select(LibraryPaper.paper_id).where(
                            LibraryPaper.library_id == library.id,
                            LibraryPaper.paper_id.in_(paper_ids),
                        )
                    )
                )
                .scalars()
                .all()
            )
        if concepts:
            existing_associations = set(
                (
                    await session.execute(
                        select(paper_concepts.c.paper_id, paper_concepts.c.concept_id).where(
                            paper_concepts.c.paper_id.in_(paper_ids),
                            paper_concepts.c.concept_id.in_(
                                [concept.id for concept in concepts.values()]
                            ),
                        )
                    )
                ).tuples()
            )

    stats.memberships_created = len(snapshot.papers) - len(existing_members)
    for source in snapshot.papers:
        paper = pool.get(source.arxiv_id)
        has_wiki = paper is not None and paper.id in existing_wikis
        if source.wiki_raw:
            if not has_wiki:
                stats.wikis_created += 1
            elif overwrite_wiki:
                stats.wikis_overwritten += 1
            else:
                stats.wikis_preserved += 1
        if copy_files:
            if source.pdf_path and not (
                paper and paper.pdf_path and Path(paper.pdf_path).is_file()
            ):
                stats.pdfs_copied += 1
            if source.text_path and not (
                paper and paper.full_text_path and Path(paper.full_text_path).is_file()
            ):
                stats.texts_copied += 1
            if source.figure_path and not (
                paper and _obsidian_figure_index(paper, source.slug) is not None
            ):
                stats.figures_copied += 1
        if index_chunks and (paper is None or paper.id not in existing_chunks):
            stats.papers_indexed += 1
        if paper is None:
            stats.concept_links_created += sum(slug in concepts for slug in source.concept_slugs)
        else:
            for slug in source.concept_slugs:
                concept = concepts.get(slug)
                if concept is not None and (paper.id, concept.id) not in existing_associations:
                    stats.concept_links_created += 1
    if dry_run:
        stats.reports_created = len(snapshot.reports)
        await session.rollback()
        return stats

    assert library is not None
    # Recompute operation counters while applying; dry-run estimates above are replaced by facts.
    stats.memberships_created = 0
    stats.wikis_created = 0
    stats.wikis_overwritten = 0
    stats.wikis_preserved = 0
    stats.pdfs_copied = 0
    stats.texts_copied = 0
    stats.figures_copied = 0
    stats.concept_links_created = 0
    stats.papers_indexed = 0

    associated: set[tuple[uuid.UUID, uuid.UUID]] = set()
    concept_ids = [concept.id for concept in concepts.values()]
    if pool and concept_ids:
        associated = set(
            (
                await session.execute(
                    select(paper_concepts.c.paper_id, paper_concepts.c.concept_id).where(
                        paper_concepts.c.paper_id.in_([paper.id for paper in pool.values()]),
                        paper_concepts.c.concept_id.in_(concept_ids),
                    )
                )
            ).tuples()
        )

    for offset, source in enumerate(snapshot.papers, start=1):
        paper = pool.get(source.arxiv_id)
        if paper is None:
            authors = [{"name": name} for name in source.authors]
            paper = new_paper(
                dedup_key=pool_dedup_key(
                    arxiv_id=source.arxiv_id,
                    doi=None,
                    title=source.title,
                    year=source.published_at.year if source.published_at else None,
                    authors=authors,
                ),
                source="arxiv",
                arxiv_id=source.arxiv_id,
                external_ids={"ArXiv": source.arxiv_id},
                title=source.title,
                authors=authors,
                abstract=source.abstract,
                year=source.published_at.year if source.published_at else None,
                url=f"https://arxiv.org/abs/{source.arxiv_id}",
                published_at=source.published_at,
                tldr=extract_tldr(source.wiki_raw),
            )
            session.add(paper)
            await session.flush()
            pool[source.arxiv_id] = paper
        else:
            _fill_missing_metadata(paper, source)

        if paper.id not in existing_members:
            session.add(
                LibraryPaper(
                    library_id=library.id,
                    paper_id=paper.id,
                    relevance_score=source.relevance_score,
                    tldr_note=extract_tldr(source.wiki_raw),
                    status="compiled" if source.wiki_raw else "included",
                )
            )
            existing_members.add(paper.id)
            stats.memberships_created += 1

        if copy_files:
            standard_pdf = papers_dir() / f"{paper.id}.pdf"
            if source.pdf_path and not (paper.pdf_path and Path(paper.pdf_path).is_file()):
                if _atomic_copy(source.pdf_path, standard_pdf):
                    stats.pdfs_copied += 1
                paper.pdf_path = str(standard_pdf)
            standard_text = papers_dir() / f"{paper.id}.txt"
            if source.text_path and not (
                paper.full_text_path and Path(paper.full_text_path).is_file()
            ):
                if _atomic_copy(source.text_path, standard_text):
                    stats.texts_copied += 1
                paper.full_text_path = str(standard_text)

        figure_index: int | None = None
        if source.figure_path and copy_files:
            current_figures = list(paper.figures or [])
            figure_index = _obsidian_figure_index(paper, source.slug)
            if figure_index is None:
                figure_index = (
                    max(
                        (int(item.get("index", -1)) for item in current_figures),
                        default=-1,
                    )
                    + 1
                )
            destination = figure_path(str(paper.id), figure_index)
            if _atomic_copy(source.figure_path, destination):
                meta = source.figure_meta or {}
                if _obsidian_figure_index(paper, source.slug) is None:
                    current_figures.append(
                        {
                            "index": figure_index,
                            "page": meta.get("page"),
                            "width": meta.get("w"),
                            "height": meta.get("h"),
                            "caption": meta.get("caption"),
                            "kind": "architecture",
                            "important": True,
                            "source": "obsidian-vault",
                            "vault_slug": source.slug,
                        }
                    )
                    paper.figures = current_figures
                stats.figures_copied += 1

        wiki = (
            await session.execute(select(PaperWiki).where(PaperWiki.paper_id == paper.id))
        ).scalar_one_or_none()
        if source.wiki_raw:
            rendered = render_markdown(
                source.wiki_raw,
                snapshot=snapshot,
                figure_index=figure_index,
            )
            if wiki is None:
                session.add(
                    PaperWiki(
                        paper_id=paper.id,
                        content=rendered,
                        model="obsidian-vault-import",
                        compiled_by=project.owner_id,
                    )
                )
                stats.wikis_created += 1
            elif overwrite_wiki:
                wiki.content = rendered
                wiki.model = "obsidian-vault-import"
                wiki.compiled_by = project.owner_id
                stats.wikis_overwritten += 1
            else:
                stats.wikis_preserved += 1

        for slug in source.concept_slugs:
            concept = concepts.get(slug)
            if concept is None:
                logger.warning("paper %s references unknown concept %s", source.slug, slug)
                continue
            key = (paper.id, concept.id)
            if key not in associated:
                await session.execute(
                    paper_concepts.insert().values(paper_id=paper.id, concept_id=concept.id)
                )
                associated.add(key)
                stats.concept_links_created += 1

        if index_chunks and paper.id not in existing_chunks:
            created = await ensure_paper_chunks(session, paper)
            if created:
                stats.papers_indexed += 1
                stats.chunks_created += created
                existing_chunks.add(paper.id)

        if offset % batch_size == 0:
            await session.commit()
            logger.info("Obsidian sync progress: %s/%s", offset, len(snapshot.papers))

    await _sync_reports(
        session,
        snapshot=snapshot,
        library=library,
        stats=stats,
        paper_ids_by_slug={
            source.slug: pool[source.arxiv_id].id
            for source in snapshot.papers
            if source.arxiv_id in pool
        },
    )
    state = dict(library.ingest_state or {})
    state["obsidian_sync"] = {
        "source": snapshot.root.name,
        "synced_at": utcnow().isoformat(),
        "paper_count": len(snapshot.papers),
    }
    library.ingest_state = state
    await session.commit()
    return stats
