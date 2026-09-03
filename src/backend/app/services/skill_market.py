"""技能市场业务逻辑（docs/skill-system.md §4.3；不 import fastapi）。

部署内共享：发布即上架（approved）→ 浏览/安装。
listing 永远指向发布时的具体 SkillVersion；安装 = 拷贝该版本为安装者的 user 技能。
"""

import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.skill import Skill, SkillListing, SkillVersion
from app.schemas.skill import SkillPublishRequest
from app.services import skills as skills_service

ACTIVE_STATUSES = ("approved",)


class ListingConflictError(Exception):
    """同一技能已有待审/在架条目。"""


class ListingStateError(Exception):
    """条目状态不允许该操作（如安装非 approved 条目）。"""


class NotOwnerError(Exception):
    """仅发布者本人（或管理员）可操作。"""


async def publish_skill(
    session: AsyncSession, skill: Skill, *, user_id: uuid.UUID, data: SkillPublishRequest
) -> SkillListing:
    """发布当前版本到市场（直接上架）。builtin 不需要发布。"""
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
        status="approved",
        summary=data.summary or skill.description,
        tags=data.tags or None,
        published_by=user_id,
    )
    session.add(listing)
    await session.commit()
    await session.refresh(listing)
    return listing


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
    """联表读数据，输出 SkillListingRead 字典列表。"""
    return [_base_read(listing) for listing in listings]


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


async def delist(
    session: AsyncSession, listing: SkillListing, *, user_id: uuid.UUID
) -> SkillListing:
    # admin 全局下架旁路已随 role 移除（#614）：只有发布者本人能下架
    if listing.published_by != user_id:
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
