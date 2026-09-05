"""OpenAlex 对齐（#639）：给缺 OpenAlex id 的论文补 id 并回填缺失元数据。

匹配顺序：DOI 精确 → arXiv id 精确（经 DataCite DOI）→ 标题+年份模糊。
openalex id 落 papers.external_ids["openalex"]（该 JSON 列本来就是给外部
id 预留的，不为此加列）；回填只补空字段——已有值一律不动，OpenAlex 的
元数据质量参差，覆盖用户手工修过的字段得不偿失。

客户端限速/缓存复用 OpenAlexClient 自身（缓存原有；限流本次补上，见
services/literature/openalex.py）。
"""

import logging
import re
from difflib import SequenceMatcher
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.models.paper import Paper
from app.services.literature import get_openalex_client
from app.services.literature.openalex import OpenAlexClient

logger = logging.getLogger(__name__)

# 标题模糊匹配阈值（归一化后 SequenceMatcher ratio）。0.92：容得下大小写/
# 标点/连字差异，容不下换了半句话的另一篇
TITLE_MATCH_RATIO = 0.92
# external_ids 里的键名
OPENALEX_KEY = "openalex"


def openalex_id_of(paper: Paper) -> str | None:
    ids = paper.external_ids if isinstance(paper.external_ids, dict) else {}
    value = ids.get(OPENALEX_KEY)
    return str(value) if value else None


def _norm_title(text: str) -> str:
    return " ".join(re.sub(r"[^0-9a-z一-鿿]+", " ", text.lower()).split())


def _short_openalex_id(raw: str) -> str:
    """https://openalex.org/W123 → W123（API 返回的是完整 URL 形态）。"""
    return raw.rstrip("/").rsplit("/", 1)[-1]


def _title_matches(a: str, b: str) -> bool:
    na, nb = _norm_title(a), _norm_title(b)
    if not na or not nb:
        return False
    return na == nb or SequenceMatcher(None, na, nb).ratio() >= TITLE_MATCH_RATIO


def _year_compatible(paper_year: int | None, meta_year: Any) -> bool:
    """年份宽容 ±1：预印本与正式发表常差一年；有一边缺年份不作为否决条件。"""
    if paper_year is None or not isinstance(meta_year, int):
        return True
    return abs(paper_year - meta_year) <= 1


def apply_openalex_meta(paper: Paper, meta: dict[str, Any]) -> bool:
    """把 OpenAlex work 元数据落到论文上：存 id + 只补空字段。返回是否有改动。"""
    changed = False
    short_id = _short_openalex_id(str(meta.get("openalex_id") or ""))
    if short_id and openalex_id_of(paper) != short_id:
        ids = dict(paper.external_ids) if isinstance(paper.external_ids, dict) else {}
        ids[OPENALEX_KEY] = short_id
        paper.external_ids = ids
        # JSON 列的字典就地换新对象，SQLAlchemy 不一定察觉，显式标脏
        flag_modified(paper, "external_ids")
        changed = True
    # 空字段回填清单：都是 OpenAlex 可靠给得出的（citation count 池表没有对应列，
    # 不为回填加列——被引数已在检索链路按需取用）
    backfills: tuple[tuple[str, Any], ...] = (
        ("doi", meta.get("doi")),
        ("abstract", meta.get("abstract")),
        ("venue", meta.get("venue")),
        ("year", meta.get("year")),
        ("url", meta.get("url")),
    )
    for field, value in backfills:
        if value and not getattr(paper, field):
            setattr(paper, field, value)
            changed = True
    if meta.get("affiliations") and not paper.affiliations:
        paper.affiliations = list(meta["affiliations"])
        changed = True
    return changed


async def align_paper(
    session: AsyncSession,  # noqa: ARG001 —— 与其余 service 签名一致，便于将来查重
    paper: Paper,
    *,
    client: OpenAlexClient | None = None,
) -> bool:
    """给一篇缺 OpenAlex id 的论文找对齐。调用方负责 commit，返回是否有改动。

    已有 id 直接返回 False（幂等）；查不到/标题对不上不写任何东西。
    """
    if openalex_id_of(paper):
        return False
    client = client or get_openalex_client()
    meta: dict[str, Any] | None = None
    if paper.doi:
        meta = await client.get_by_doi(paper.doi)
    elif paper.arxiv_id:
        meta = await client.get_by_arxiv(paper.arxiv_id)
    elif paper.title:
        year = paper.year
        candidates = await client.search_works(
            paper.title,
            limit=5,
            start_year=year - 1 if year else None,
            end_year=year + 1 if year else None,
        )
        meta = next(
            (
                c
                for c in candidates
                if _title_matches(paper.title, str(c.get("title") or ""))
                and _year_compatible(year, c.get("year"))
            ),
            None,
        )
    if not meta or not meta.get("openalex_id"):
        return False
    # 精确通道（DOI/arXiv）拿回来的也复核一下标题：DOI 录错时宁可不对齐
    if meta.get("title") and paper.title and not _title_matches(paper.title, str(meta["title"])):
        logger.info("openalex alignment rejected by title mismatch for paper %s", paper.id)
        return False
    return apply_openalex_meta(paper, meta)


async def align_missing_papers(
    session: AsyncSession,
    *,
    client: OpenAlexClient | None = None,
    limit: int | None = None,
) -> dict[str, int]:
    """批量对齐（CLI 全库回填用）：逐篇 commit，单篇失败不拖垮整批。"""
    stmt = select(Paper).order_by(Paper.created_at)
    if limit:
        stmt = stmt.limit(limit)
    stats = {"scanned": 0, "aligned": 0, "failed": 0}
    for paper in (await session.execute(stmt)).scalars():
        if openalex_id_of(paper):
            continue
        stats["scanned"] += 1
        try:
            if await align_paper(session, paper, client=client):
                await session.commit()
                stats["aligned"] += 1
        except Exception:  # noqa: BLE001 — 单篇尽力而为
            logger.warning("openalex alignment failed for paper %s", paper.id, exc_info=True)
            await session.rollback()
            stats["failed"] += 1
    return stats
