"""技能市场业务逻辑（docs/skill-system.md §4.3；不 import fastapi）。

部署内共享：发布（pending）→ 管理员审核（approved/rejected）→ 浏览/安装/评分。
listing 永远指向发布时的具体 SkillVersion；安装 = 拷贝该版本为安装者的 user 技能。
"""

import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.skill import Skill, SkillListing, SkillRating, SkillVersion
from app.schemas.skill import SkillPublishRequest, SkillRatingCreate
from app.services import skills as skills_service

ACTIVE_STATUSES = ("pending", "approved")


class ListingConflictError(Exception):
    """同一技能已有待审/在架条目。"""


class ListingStateError(Exception):
    """条目状态不允许该操作（如审核非 pending、安装非 approved）。"""


class NotOwnerError(Exception):
    """仅发布者本人（或管理员）可操作。"""


async def publish_skill(
    session: AsyncSession, skill: Skill, *, user_id: uuid.UUID, data: SkillPublishRequest
) -> SkillListing:
    """发布当前版本到市场（pending，待管理员审核）。builtin 不需要发布。"""
    if skill.scope == "builtin" or skill.owner_id != user_id:
        raise NotOwnerError(skill.slug)
    version = await skills_service.latest_version(session, skill.id)
    if version is None:
        raise ListingStateError(f"{skill.slug} has no version")
    stmt = select(SkillListing.id).where(
        SkillListing.skill_id == skill.id, SkillListing.status.in_(ACTIVE_STATUSES)
    )
    if (await session.execute(stmt)).first() is not None:
        raise ListingConflictError(skill.slug)
    listing = SkillListing(
        skill_id=skill.id,
        skill_version_id=version.id,
        summary=data.summary or skill.description,
        tags=data.tags or None,
        published_by=user_id,
    )
    session.add(listing)
    await session.commit()
    await session.refresh(listing)
    return listing


async def _rating_stats(
    session: AsyncSession, listing_ids: list[uuid.UUID]
) -> dict[uuid.UUID, tuple[float, int]]:
    if not listing_ids:
        return {}
    stmt = (
        select(SkillRating.listing_id, func.avg(SkillRating.rating), func.count())
        .where(SkillRating.listing_id.in_(listing_ids))
        .group_by(SkillRating.listing_id)
    )
    return {
        lid: (round(float(avg), 2), int(count))
        for lid, avg, count in (await session.execute(stmt)).all()
    }


def _base_read(listing: SkillListing) -> dict[str, Any]:
    # 手工组装：SkillListingRead.version（版本号 int）与 ORM 关系 listing.version
    # （SkillVersion 对象）同名，不能走 from_attributes
    from app.schemas.skill import SkillListingRead, SkillRead

    read = SkillListingRead(
        id=listing.id,
        skill_id=listing.skill_id,
        skill_version_id=listing.skill_version_id,
        summary=listing.summary,
        tags=listing.tags,
        status=listing.status,
        install_count=listing.install_count,
        published_by=listing.published_by,
        comment=listing.comment,
        created_at=listing.created_at,
        skill=SkillRead.model_validate(listing.skill) if listing.skill is not None else None,
        version=listing.version.version if listing.version is not None else None,
    )
    return read.model_dump()


async def annotate_listings(
    session: AsyncSession, listings: Sequence[SkillListing]
) -> list[dict[str, Any]]:
    """联表读数据 + 评分聚合，输出 SkillListingRead 字典列表。"""
    stats = await _rating_stats(session, [listing.id for listing in listings])
    out = []
    for listing in listings:
        data = _base_read(listing)
        avg_count = stats.get(listing.id)
        if avg_count:
            data["rating_avg"], data["rating_count"] = avg_count
        out.append(data)
    return out


def _listing_query():
    return select(SkillListing).options(
        selectinload(SkillListing.skill), selectinload(SkillListing.version)
    )


async def list_market(
    session: AsyncSession,
    *,
    status: str = "approved",
    q: str | None = None,
    sort: str = "-created_at",
) -> Sequence[SkillListing]:
    stmt = _listing_query().where(SkillListing.status == status)
    if q:
        pattern = f"%{q}%"
        stmt = stmt.join(Skill, SkillListing.skill_id == Skill.id).where(
            Skill.name.ilike(pattern) | Skill.slug.ilike(pattern)
        )
    if sort == "installs":
        stmt = stmt.order_by(SkillListing.install_count.desc(), SkillListing.created_at.desc())
    else:
        stmt = stmt.order_by(SkillListing.created_at.desc())
    return (await session.execute(stmt)).scalars().all()


async def get_listing(session: AsyncSession, listing_id: uuid.UUID) -> SkillListing | None:
    stmt = _listing_query().where(SkillListing.id == listing_id)
    return (await session.execute(stmt)).scalar_one_or_none()


async def decide_listing(
    session: AsyncSession,
    listing: SkillListing,
    *,
    approved: bool,
    decided_by: uuid.UUID,
    comment: str | None = None,
) -> SkillListing:
    if listing.status != "pending":
        raise ListingStateError(listing.status)
    listing.status = "approved" if approved else "rejected"
    listing.decided_by = decided_by
    listing.comment = comment
    await session.commit()
    await session.refresh(listing)
    return listing


async def delist(
    session: AsyncSession, listing: SkillListing, *, user_id: uuid.UUID, is_admin: bool
) -> SkillListing:
    if not is_admin and listing.published_by != user_id:
        raise NotOwnerError(str(listing.id))
    listing.status = "delisted"
    await session.commit()
    await session.refresh(listing)
    return listing


async def install_listing(
    session: AsyncSession, listing: SkillListing, *, user_id: uuid.UUID
) -> Skill:
    """安装 = 拷贝发布版本为安装者的 user 技能（slug 冲突自动加后缀）。"""
    if listing.status != "approved":
        raise ListingStateError(listing.status)
    version = await session.get(SkillVersion, listing.skill_version_id)
    src_skill = listing.skill or await session.get(Skill, listing.skill_id)
    if version is None or src_skill is None:
        raise ListingStateError("listing source missing")
    installed = await _copy_as_user_skill(session, src_skill, version, user_id=user_id)
    listing.install_count += 1
    await session.commit()
    await session.refresh(installed)
    return installed


async def _copy_as_user_skill(
    session: AsyncSession, src: Skill, version: SkillVersion, *, user_id: uuid.UUID
) -> Skill:
    slug = src.slug
    if await skills_service._slug_taken(session, slug, owner_id=user_id, scope="user"):  # noqa: SLF001
        for i in range(2, 100):
            candidate = f"{src.slug}-{i}"[:64]
            if not await skills_service._slug_taken(  # noqa: SLF001
                session, candidate, owner_id=user_id, scope="user"
            ):
                slug = candidate
                break
        else:  # pragma: no cover — 防御分支
            raise ListingStateError(f"no available slug for {src.slug}")
    skill = Skill(
        slug=slug,
        kind=src.kind,
        name=src.name,
        name_en=src.name_en,
        description=src.description,
        scope="user",
        owner_id=user_id,
    )
    session.add(skill)
    await session.flush()
    session.add(
        SkillVersion(
            skill_id=skill.id,
            version=1,
            manifest=version.manifest,
            body=version.body,
            changelog=f"从技能市场安装（{src.slug} v{version.version}）",
            created_by=user_id,
        )
    )
    return skill


async def upsert_rating(
    session: AsyncSession, listing: SkillListing, *, user_id: uuid.UUID, data: SkillRatingCreate
) -> SkillRating:
    if listing.status != "approved":
        raise ListingStateError(listing.status)
    stmt = select(SkillRating).where(
        SkillRating.listing_id == listing.id, SkillRating.user_id == user_id
    )
    rating = (await session.execute(stmt)).scalar_one_or_none()
    if rating is None:
        rating = SkillRating(listing_id=listing.id, user_id=user_id)
        session.add(rating)
    rating.rating = data.rating
    rating.comment = data.comment
    await session.commit()
    await session.refresh(rating)
    return rating


async def list_ratings(session: AsyncSession, listing_id: uuid.UUID) -> Sequence[SkillRating]:
    stmt = (
        select(SkillRating)
        .where(SkillRating.listing_id == listing_id)
        .order_by(SkillRating.created_at.desc())
    )
    return (await session.execute(stmt)).scalars().all()
