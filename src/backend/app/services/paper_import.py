"""手动添加文献：arxiv_id / doi / bibtex 三选一（docs/api-lit.md §4，不 import fastapi）。"""

import asyncio
import logging
import re
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.paper import Paper
from app.services.dedup import pool_dedup_key
from app.services.libraries import (
    ensure_membership,
    find_pool_paper,
    get_library_for_project,
    get_membership,
)
from app.services.literature import get_arxiv_client, get_openalex_client
from app.services.literature.arxiv import normalize_arxiv_id

logger = logging.getLogger(__name__)

_BRACES_RE = re.compile(r"[{}]")
_WS_RE = re.compile(r"\s+")


class ParseFailedError(Exception):
    """来源解析失败（arxiv/DOI 查不到、bibtex 不合法等），str(e) 为原因。"""


class DuplicatePaperError(Exception):
    """本方向库内已有同一篇论文（内容池命中且成员行已存在）。"""

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
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    # OpenAlex 的 publication_date 是纯日期，parse 出来无时区 → 按 UTC 处理
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


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
        "published_at": _parse_iso(meta.get("published")),
    }


async def add_manual_paper(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    arxiv_id: str | None = None,
    doi: str | None = None,
    bibtex: str | None = None,
) -> Paper:
    """手动添加一篇文献（source=manual，成员行 status=included）。

    - 先查全局内容池（arxiv/doi/dedup_key）：池中已有则只建成员行（pool hit，
      跳过解析下载）；本库已有成员行 → DuplicatePaperError（路由映射 409）
    - 解析失败 → ParseFailedError（路由映射 422）
    - 新论文有 arxiv_id 的自动尝试补下 PDF，失败只记日志不阻塞
    """
    if arxiv_id:
        fields = await _fields_from_arxiv(arxiv_id)
    elif doi:
        fields = await _fields_from_doi(doi)
    else:
        fields = parse_bibtex_entry(bibtex or "")

    library = await get_library_for_project(session, project_id)
    dedup_key = pool_dedup_key(
        arxiv_id=fields.get("arxiv_id"),
        doi=fields.get("doi"),
        title=fields["title"],
        year=fields.get("year"),
        authors=fields.get("authors"),
    )
    pooled = await find_pool_paper(
        session, arxiv_id=fields.get("arxiv_id"), doi=fields.get("doi"), dedup_key=dedup_key
    )
    if pooled is not None:
        if await get_membership(session, library_id=library.id, paper_id=pooled.id) is not None:
            raise DuplicatePaperError(pooled.id)
        logger.info("paper pool hit for manual import: %s", pooled.id)
        await ensure_membership(
            session, library_id=library.id, paper_id=pooled.id, status="included"
        )
        await session.commit()
        await session.refresh(pooled)
        return pooled

    external_ids: dict[str, str] = {}
    if fields.get("arxiv_id"):
        external_ids["arxiv"] = fields["arxiv_id"]
    if fields.get("doi"):
        external_ids["doi"] = fields["doi"]
    paper = Paper(
        source="manual",
        dedup_key=dedup_key,
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
    await ensure_membership(session, library_id=library.id, paper_id=paper.id, status="included")

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

    # 发表机构：全文到手后 LLM 从标题页解析（DOI 路径已有 OpenAlex 机构时不覆盖）；
    # 失败不影响创建
    if not paper.affiliations and paper.full_text_path:
        from app.core.llm.router import get_llm_router
        from app.services.affiliations import extract_affiliations_llm

        affs = await extract_affiliations_llm(paper, llm=get_llm_router(), project_id=project_id)
        if affs:
            paper.affiliations = affs

    await session.commit()
    await session.refresh(paper)
    return paper
