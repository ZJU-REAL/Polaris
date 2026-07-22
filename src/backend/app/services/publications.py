"""我发表的论文：作者身份绑定、文献库「姓名+机构」匹配产候选、确认/驳回、手动补录。

不从外部学术库自动拉取（不可靠）；候选只来自两处——手动补录，以及平台文献库
（每日 ingest 入库 + 手动扫描）里作者姓名与绑定变体命中、机构不冲突的论文。
所有库内候选都要经用户确认（pending → confirmed | rejected）。
"""

import logging
import re
import uuid
from typing import Any, cast

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.base import utcnow
from app.models.library_direction import DirectionLibrary, LibraryPaper
from app.models.paper import Paper
from app.models.project import ProjectMember
from app.models.publication import UserAuthorProfile, UserPublication
from app.services.dedup import dedup_key_for as shared_dedup_key_for
from app.services.paper_import import (
    ParseFailedError,
    _fields_from_arxiv,
    _fields_from_doi,
    parse_bibtex_entry,
)

logger = logging.getLogger(__name__)


def _normalize_name(name: str) -> str:
    return re.sub(r"[^a-z一-鿿0-9]+", " ", name.lower()).strip()


def dedup_key_for(*, doi: str | None, arxiv_id: str | None, title: str) -> str:
    """去重键：doi → arxiv → 规范化标题 sha1（历史键 doi 优先，顺序不可改）。"""
    key = shared_dedup_key_for(
        doi=doi, arxiv_id=arxiv_id, title=title, priority=("doi", "arxiv", "title")
    )
    assert key is not None  # title 非空
    return key


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


# ---- 文献库匹配 ----


def _name_matches(author_name: str, variants: set[str]) -> bool:
    """规范化后精确匹配；兼容「姓 名」与「名 姓」两种词序。"""
    norm = _normalize_name(author_name)
    if not norm:
        return False
    reversed_norm = " ".join(reversed(norm.split()))
    return norm in variants or reversed_norm in variants


def _affiliation_compatible(paper_affils: list[Any] | None, profile_affils: list[str]) -> bool:
    """机构门槛：论文带机构信息时须与绑定机构有交集（包含匹配，双向）；

    论文没有机构信息（未 enrich）或绑定未填机构时不设门槛——反正候选都要人工确认。
    """
    papers = [a.lower() for a in (paper_affils or []) if isinstance(a, str) and a.strip()]
    mine = [a.lower() for a in profile_affils if a.strip()]
    if not papers or not mine:
        return True
    return any(m in p or p in m for p in papers for m in mine)


def paper_matches_profile(paper: Paper, profile: UserAuthorProfile) -> bool:
    variants = {_normalize_name(v) for v in profile.name_variants if _normalize_name(v)}
    if not variants:
        return False
    authors = paper.authors or []
    names = [a.get("name") for a in authors if isinstance(a, dict) and a.get("name")]
    if not any(_name_matches(n, variants) for n in names):
        return False
    return _affiliation_compatible(paper.affiliations, profile.affiliations)


def _publication_from_paper(user_id: uuid.UUID, paper: Paper) -> UserPublication:
    return UserPublication(
        user_id=user_id,
        dedup_key=dedup_key_for(doi=paper.doi, arxiv_id=paper.arxiv_id, title=paper.title),
        arxiv_id=paper.arxiv_id,
        doi=paper.doi,
        title=paper.title,
        authors=paper.authors,
        year=paper.year,
        venue=paper.venue,
        url=paper.url,
        paper_id=paper.id,
        source="library",
        status="pending",
    )


async def _existing_dedup_keys(session: AsyncSession, user_id: uuid.UUID) -> set[str]:
    stmt = select(UserPublication.dedup_key).where(UserPublication.user_id == user_id)
    return set((await session.execute(stmt)).scalars().all())


async def match_from_library(session: AsyncSession, *, user_id: uuid.UUID) -> int:
    """扫描该用户所在方向的文献库，姓名+机构命中的论文入 pending 候选。

    已存在的条目（含 rejected）一律跳过——驳回过的不再打扰；返回新增候选数。
    每日 cron 与手动触发共用；全量扫描 + 去重键幂等，重复跑无副作用。
    """
    profile = await get_profile(session, user_id=user_id)
    if profile is None:
        return 0
    stmt = (
        select(Paper)
        .distinct()
        .join(LibraryPaper, LibraryPaper.paper_id == Paper.id)
        .join(DirectionLibrary, DirectionLibrary.id == LibraryPaper.library_id)
        .join(ProjectMember, ProjectMember.project_id == DirectionLibrary.project_id)
        .where(ProjectMember.user_id == user_id, LibraryPaper.status != "excluded")
    )
    papers = (await session.execute(stmt)).scalars().all()
    seen = await _existing_dedup_keys(session, user_id)
    added = 0
    for paper in papers:
        if not paper_matches_profile(paper, profile):
            continue
        pub = _publication_from_paper(user_id, paper)
        if pub.dedup_key in seen:
            continue
        seen.add(pub.dedup_key)
        session.add(pub)
        added += 1
    profile.last_synced_at = utcnow()
    await session.commit()
    return added


async def profiles_for_daily_match(session: AsyncSession) -> list[uuid.UUID]:
    """每日自动匹配的对象：开了 auto_sync 的绑定用户。"""
    stmt = select(UserAuthorProfile.user_id).where(UserAuthorProfile.auto_sync.is_(True))
    return list((await session.execute(stmt)).scalars().all())


# ---- 发表记录 ----


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
    # 确认列表按年份展示；待确认队列按新发现在前
    if status == "confirmed":
        stmt = stmt.order_by(
            UserPublication.year.desc().nulls_last(), UserPublication.created_at.desc()
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
