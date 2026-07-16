"""手动添加文献：arxiv_id / doi / bibtex 三选一（docs/api-lit.md §4，不 import fastapi）。"""

import asyncio
import logging
import re
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.paper import Paper
from app.services.literature import get_arxiv_client, get_openalex_client
from app.services.literature.arxiv import normalize_arxiv_id

logger = logging.getLogger(__name__)

_BRACES_RE = re.compile(r"[{}]")
_WS_RE = re.compile(r"\s+")


class ParseFailedError(Exception):
    """来源解析失败（arxiv/DOI 查不到、bibtex 不合法等），str(e) 为原因。"""


class DuplicatePaperError(Exception):
    """项目内已有同 arxiv_id / doi 的论文。"""

    def __init__(self, paper_id: uuid.UUID) -> None:
        super().__init__(str(paper_id))
        self.paper_id = paper_id


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = _WS_RE.sub(" ", _BRACES_RE.sub("", value)).strip()
    return cleaned or None


def parse_bibtex_entry(bibtex: str) -> dict[str, Any]:
    """解析单条 bibtex 条目 → Paper 字段（title 必需；author/year/venue/doi/url 尽量取）。"""
    import bibtexparser
    from bibtexparser.bparser import BibTexParser

    try:
        parser = BibTexParser(common_strings=True)
        parser.ignore_nonstandard_types = False
        db = bibtexparser.loads(bibtex, parser=parser)
    except Exception as e:  # noqa: BLE001 — bibtexparser 解析异常统一归为解析失败
        raise ParseFailedError(f"bibtex 解析出错（{type(e).__name__}）") from e
    if not db.entries:
        raise ParseFailedError("bibtex 里没有可识别的条目")
    entry = db.entries[0]
    title = _clean(entry.get("title"))
    if not title:
        raise ParseFailedError("bibtex 条目缺少 title")
    authors = [
        {"name": name}
        for raw in (entry.get("author") or "").split(" and ")
        if (name := _clean(raw))
    ]
    year: int | None = None
    year_match = re.search(r"\d{4}", entry.get("year") or "")
    if year_match:
        year = int(year_match.group())
    venue = _clean(entry.get("journal") or entry.get("booktitle"))
    arxiv_id = None
    if entry.get("eprint") and "arxiv" in (entry.get("archiveprefix") or "arxiv").lower():
        arxiv_id = normalize_arxiv_id(entry["eprint"])
    return {
        "title": title,
        "authors": authors,
        "year": year,
        "venue": venue,
        "doi": _clean(entry.get("doi")),
        "url": _clean(entry.get("url")),
        "arxiv_id": arxiv_id,
        "abstract": _clean(entry.get("abstract")),
    }


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


async def _fields_from_arxiv(arxiv_id: str) -> dict[str, Any]:
    normalized = normalize_arxiv_id(arxiv_id)
    entries = await get_arxiv_client().fetch_by_ids([normalized])
    entry = next((e for e in entries if e.get("title")), None)
    if entry is None:
        raise ParseFailedError(f"arxiv 上查不到编号 {normalized}")
    return {
        "title": entry["title"],
        "authors": entry.get("authors"),
        "abstract": entry.get("abstract"),
        "year": entry.get("year"),
        "venue": entry.get("primary_category"),
        "doi": entry.get("doi"),
        "url": entry.get("url"),
        "arxiv_id": entry.get("arxiv_id") or normalized,
        "published_at": _parse_iso(entry.get("published")),
    }


async def _fields_from_doi(doi: str) -> dict[str, Any]:
    doi = doi.strip().removeprefix("https://doi.org/")
    meta = await get_openalex_client().get_by_doi(doi)
    if meta is None or not meta.get("title"):
        raise ParseFailedError(f"OpenAlex 上查不到 DOI {doi}")
    return {
        "title": meta["title"],
        "authors": meta.get("authors"),
        "affiliations": meta.get("affiliations") or [],
        "year": meta.get("year"),
        "venue": meta.get("venue"),
        "doi": meta.get("doi") or doi,
        "url": meta.get("url") or f"https://doi.org/{doi}",
    }


async def _find_duplicate(
    session: AsyncSession, *, project_id: uuid.UUID, arxiv_id: str | None, doi: str | None
) -> uuid.UUID | None:
    conditions = []
    if arxiv_id:
        conditions.append(Paper.arxiv_id == arxiv_id)
    if doi:
        conditions.append(func.lower(Paper.doi) == doi.lower())
    if not conditions:
        return None
    stmt = select(Paper.id).where(Paper.project_id == project_id, or_(*conditions)).limit(1)
    return (await session.execute(stmt)).scalar_one_or_none()


async def add_manual_paper(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    arxiv_id: str | None = None,
    doi: str | None = None,
    bibtex: str | None = None,
) -> Paper:
    """手动添加一篇文献（source=manual, status=included）。

    - 项目内按 arxiv_id / doi 去重 → DuplicatePaperError（路由映射 409）
    - 解析失败 → ParseFailedError（路由映射 422）
    - 有 arxiv_id 的自动尝试补下 PDF，失败只记日志不阻塞
    """
    if arxiv_id:
        fields = await _fields_from_arxiv(arxiv_id)
    elif doi:
        fields = await _fields_from_doi(doi)
    else:
        fields = parse_bibtex_entry(bibtex or "")

    dup_id = await _find_duplicate(
        session, project_id=project_id, arxiv_id=fields.get("arxiv_id"), doi=fields.get("doi")
    )
    if dup_id is not None:
        raise DuplicatePaperError(dup_id)

    external_ids: dict[str, str] = {}
    if fields.get("arxiv_id"):
        external_ids["arxiv"] = fields["arxiv_id"]
    if fields.get("doi"):
        external_ids["doi"] = fields["doi"]
    paper = Paper(
        project_id=project_id,
        source="manual",
        status="included",
        arxiv_id=fields.get("arxiv_id"),
        doi=fields.get("doi"),
        external_ids=external_ids or None,
        title=fields["title"],
        authors=fields.get("authors"),
        affiliations=fields.get("affiliations"),
        abstract=fields.get("abstract"),
        year=fields.get("year"),
        venue=fields.get("venue"),
        url=fields.get("url"),
        published_at=fields.get("published_at"),
    )
    session.add(paper)
    await session.flush()

    if paper.arxiv_id:
        # 尽力而为补下 PDF + 抽全文；失败只记日志，不阻塞创建
        from app.services.literature.pdf_extract import extract_full_text, save_pdf

        try:
            content = await get_arxiv_client().download_pdf(paper.arxiv_id)
            pdf_path = save_pdf(str(paper.id), content)
            paper.pdf_path = str(pdf_path)
            txt_path = await extract_full_text(str(paper.id), pdf_path)
            paper.full_text_path = str(txt_path)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.warning("auto PDF fetch failed for paper %s", paper.id, exc_info=True)

    await session.commit()
    await session.refresh(paper)
    return paper
