"""我发表的论文：作者身份绑定、OpenAlex 同步产候选、确认/驳回、手动补录。"""

import hashlib
import logging
import re
import uuid
from typing import Any, cast

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.base import utcnow
from app.models.publication import UserAuthorProfile, UserPublication
from app.services.literature import get_openalex_client
from app.services.paper_import import (
    ParseFailedError,
    _fields_from_arxiv,
    _fields_from_doi,
    parse_bibtex_entry,
)

logger = logging.getLogger(__name__)

_ARXIV_DOI_RE = re.compile(r"^10\.48550/arxiv\.(.+)$", re.IGNORECASE)


def _normalize_title(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()


def _arxiv_id_from_doi(doi: str | None) -> str | None:
    if not doi:
        return None
    match = _ARXIV_DOI_RE.match(doi.strip())
    return match.group(1) if match else None


def dedup_key_for(*, doi: str | None, arxiv_id: str | None, title: str) -> str:
    """去重键：doi → arxiv → 规范化标题 sha1（arXiv 的 DataCite DOI 归一到 doi 档）。"""
    if doi:
        return f"doi:{doi.lower()}"
    if arxiv_id:
        return f"arxiv:{arxiv_id.lower()}"
    return f"title:{hashlib.sha1(_normalize_title(title).encode()).hexdigest()}"


# ---- 作者身份绑定 ----


async def get_profile(session: AsyncSession, *, user_id: uuid.UUID) -> UserAuthorProfile | None:
    stmt = select(UserAuthorProfile).where(UserAuthorProfile.user_id == user_id)
    return (await session.execute(stmt)).scalar_one_or_none()


async def upsert_profile(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    name_variants: list[str],
    affiliations: list[str],
    openalex_author_id: str | None,
    orcid: str | None,
    auto_sync: bool,
) -> UserAuthorProfile:
    profile = await get_profile(session, user_id=user_id)
    if profile is None:
        profile = UserAuthorProfile(user_id=user_id, name_variants=[], affiliations=[])
        session.add(profile)
    profile.name_variants = name_variants
    profile.affiliations = affiliations
    profile.openalex_author_id = openalex_author_id
    profile.orcid = orcid
    profile.auto_sync = auto_sync
    await session.commit()
    await session.refresh(profile)
    return profile


async def author_candidates(name: str, affiliation: str | None) -> list[dict[str, Any]]:
    """OpenAlex 作者实体候选；给了机构时把机构命中的排前面（实体选择由用户完成）。"""
    candidates = await get_openalex_client().search_authors(name)
    if affiliation:
        needle = affiliation.lower()
        candidates.sort(
            key=lambda c: not any(needle in (a or "").lower() for a in c["affiliations"])
        )
    return candidates


# ---- 发表记录 ----


async def _existing_dedup_keys(session: AsyncSession, user_id: uuid.UUID) -> set[str]:
    stmt = select(UserPublication.dedup_key).where(UserPublication.user_id == user_id)
    return set((await session.execute(stmt)).scalars().all())


def _publication_from_work(user_id: uuid.UUID, work: dict[str, Any]) -> UserPublication:
    doi = work.get("doi")
    arxiv_id = _arxiv_id_from_doi(doi)
    return UserPublication(
        user_id=user_id,
        dedup_key=dedup_key_for(doi=doi, arxiv_id=arxiv_id, title=work["title"]),
        openalex_id=(work.get("openalex_id") or "").rsplit("/", 1)[-1] or None,
        arxiv_id=arxiv_id,
        doi=doi,
        title=work["title"],
        authors=work.get("authors"),
        year=work.get("year"),
        venue=work.get("venue"),
        url=work.get("url"),
        cited_by_count=work.get("cited_by_count"),
        source="openalex",
        status="pending",
    )


async def sync_publications(session: AsyncSession, *, user_id: uuid.UUID) -> int:
    """按绑定的 OpenAlex 作者实体拉全部 works，新论文入 pending 候选。

    已存在的条目（含 rejected）一律跳过——驳回过的不再打扰；返回新增候选数。
    """
    profile = await get_profile(session, user_id=user_id)
    if profile is None or not profile.openalex_author_id:
        return 0
    works = await get_openalex_client().works_by_author(profile.openalex_author_id)
    seen = await _existing_dedup_keys(session, user_id)
    added = 0
    for work in works:
        if not work.get("title"):
            continue
        pub = _publication_from_work(user_id, work)
        if pub.dedup_key in seen:
            continue
        seen.add(pub.dedup_key)
        session.add(pub)
        added += 1
    profile.last_synced_at = utcnow()
    await session.commit()
    return added


async def add_manual_publication(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    arxiv_id: str | None,
    doi: str | None,
    bibtex: str | None,
) -> UserPublication:
    """手动补录（arxiv_id | doi | bibtex 三选一），直接 confirmed。

    已有同一篇时改置 confirmed（把误驳回/待确认的捞回来），不重复建行。
    Raises:
        ParseFailedError: 元数据解析失败。
    """
    if arxiv_id:
        fields = await _fields_from_arxiv(arxiv_id)
    elif doi:
        fields = await _fields_from_doi(doi)
    elif bibtex:
        fields = parse_bibtex_entry(bibtex)
    else:  # schema 层已校验三选一，这里兜底
        raise ParseFailedError("需要 arxiv_id / doi / bibtex 之一")
    key = dedup_key_for(
        doi=fields.get("doi"), arxiv_id=fields.get("arxiv_id"), title=fields["title"]
    )
    stmt = select(UserPublication).where(
        UserPublication.user_id == user_id, UserPublication.dedup_key == key
    )
    pub = (await session.execute(stmt)).scalar_one_or_none()
    if pub is None:
        pub = UserPublication(
            user_id=user_id,
            dedup_key=key,
            arxiv_id=fields.get("arxiv_id"),
            doi=fields.get("doi"),
            title=fields["title"],
            authors=fields.get("authors"),
            year=fields.get("year"),
            venue=fields.get("venue"),
            url=fields.get("url"),
            source="manual",
        )
        session.add(pub)
    return await set_status(session, publication=pub, status="confirmed")


async def get_publication(
    session: AsyncSession, *, user_id: uuid.UUID, publication_id: uuid.UUID
) -> UserPublication | None:
    pub = await session.get(UserPublication, publication_id)
    if pub is None or pub.user_id != user_id:
        return None
    return pub


async def set_status(
    session: AsyncSession, *, publication: UserPublication, status: str
) -> UserPublication:
    publication.status = status
    publication.confirmed_at = utcnow() if status == "confirmed" else None
    await session.commit()
    await session.refresh(publication)
    return publication


async def list_publications(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    status: str = "confirmed",
    page: int = 1,
    size: int = 20,
) -> tuple[list[UserPublication], int]:
    stmt = select(UserPublication).where(
        UserPublication.user_id == user_id, UserPublication.status == status
    )
    # 确认列表按年份/被引数展示；待确认队列按新发现在前
    if status == "confirmed":
        stmt = stmt.order_by(
            UserPublication.year.desc().nulls_last(), UserPublication.cited_by_count.desc()
        )
    else:
        stmt = stmt.order_by(UserPublication.created_at.desc())
    total = cast(
        int,
        (await session.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one(),
    )
    rows = (await session.execute(stmt.offset((page - 1) * size).limit(size))).scalars().all()
    return list(rows), total


async def status_counts(session: AsyncSession, *, user_id: uuid.UUID) -> dict[str, int]:
    stmt = (
        select(UserPublication.status, func.count())
        .where(UserPublication.user_id == user_id)
        .group_by(UserPublication.status)
    )
    counts = {row[0]: row[1] for row in (await session.execute(stmt)).all()}
    return {s: counts.get(s, 0) for s in ("pending", "confirmed", "rejected")}
