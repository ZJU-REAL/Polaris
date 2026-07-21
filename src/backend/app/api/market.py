"""技能市场路由（docs/skill-system.md §4.3）。

- 发布：POST /skills/{id}/publish（技能主人）→ pending，管理员审核
- 浏览/详情/安装/评分：登录即可（部署内共享）
- 审核 approve/reject：仅管理员；下架：发布者或管理员
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import current_active_user, require_admin
from app.core.db import get_session
from app.models.skill import SkillListing, SkillVersion
from app.models.user import User
from app.schemas.skill import (
    ListingDecision,
    SkillDetail,
    SkillListingDetail,
    SkillListingRead,
    SkillPublishRequest,
    SkillRatingCreate,
    SkillRatingRead,
)
from app.services import skill_market as market_service
from app.services import skills as skills_service

router = APIRouter(tags=["skill-market"])


async def _get_listing(session: AsyncSession, listing_id: uuid.UUID) -> SkillListing:
    listing = await market_service.get_listing(session, listing_id)
    if listing is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="LISTING_NOT_FOUND")
    return listing


@router.post(
    "/skills/{skill_id}/publish",
    response_model=SkillListingRead,
    status_code=status.HTTP_201_CREATED,
)
async def publish_skill(
    skill_id: uuid.UUID,
    data: SkillPublishRequest | None = None,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> SkillListingRead:
    skill = await skills_service.get_skill(session, skill_id, user_id=user.id)
    if skill is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="SKILL_NOT_FOUND")
    try:
        listing = await market_service.publish_skill(
            session, skill, user_id=user.id, data=data or SkillPublishRequest()
        )
    except market_service.NotOwnerError as e:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="NOT_SKILL_OWNER") from e
    except market_service.ListingConflictError as e:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="ALREADY_LISTED") from e
    except market_service.ListingStateError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e)) from e
    listing = await _get_listing(session, listing.id)  # 联表字段
    return SkillListingRead(**(await market_service.annotate_listings(session, [listing]))[0])


@router.get("/market/skills", response_model=list[SkillListingRead])
async def list_market(
    q: str | None = Query(default=None, max_length=100),
    sort: str = Query(default="-created_at", pattern="^(-created_at|installs)$"),
    status_filter: str = Query(default="approved", alias="status", pattern="^(approved|pending)$"),
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> list[SkillListingRead]:
    """市场列表。status=pending 为管理员审核队列（非管理员 403）。"""
    if status_filter == "pending" and user.role != "admin":
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="ADMIN_REQUIRED")
    listings = await market_service.list_market(session, status=status_filter, q=q, sort=sort)
    return [
        SkillListingRead(**d) for d in await market_service.annotate_listings(session, listings)
    ]


@router.get("/market/skills/{listing_id}", response_model=SkillListingDetail)
async def get_listing(
    listing_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> SkillListingDetail:
    """详情含发布版本全文（安装前强制可预览）。"""
    listing = await _get_listing(session, listing_id)
    data = (await market_service.annotate_listings(session, [listing]))[0]
    detail = SkillListingDetail(**data)
    version = await session.get(SkillVersion, listing.skill_version_id)
    if version is not None:
        detail.manifest = version.manifest
        detail.body = version.body
    return detail


@router.post("/market/skills/{listing_id}/install", response_model=SkillDetail, status_code=201)
async def install_listing(
    listing_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> SkillDetail:
    listing = await _get_listing(session, listing_id)
    try:
        skill = await market_service.install_listing(session, listing, user_id=user.id)
    except market_service.ListingStateError as e:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="LISTING_NOT_APPROVED") from e
    version = await skills_service.latest_version(session, skill.id)
    from app.schemas.skill import SkillVersionRead

    detail = SkillDetail.model_validate(skill)
    detail.current_version = SkillVersionRead.model_validate(version) if version else None
    return detail


@router.post("/market/skills/{listing_id}/approve", response_model=SkillListingRead)
async def approve_listing(
    listing_id: uuid.UUID,
    data: ListingDecision | None = None,
    session: AsyncSession = Depends(get_session),
    admin: User = Depends(require_admin),
) -> SkillListingRead:
    return await _decide(session, listing_id, admin, approved=True, data=data)


@router.post("/market/skills/{listing_id}/reject", response_model=SkillListingRead)
async def reject_listing(
    listing_id: uuid.UUID,
    data: ListingDecision | None = None,
    session: AsyncSession = Depends(get_session),
    admin: User = Depends(require_admin),
) -> SkillListingRead:
    return await _decide(session, listing_id, admin, approved=False, data=data)


async def _decide(
    session: AsyncSession,
    listing_id: uuid.UUID,
    admin: User,
    *,
    approved: bool,
    data: ListingDecision | None,
) -> SkillListingRead:
    listing = await _get_listing(session, listing_id)
    try:
        listing = await market_service.decide_listing(
            session,
            listing,
            approved=approved,
            decided_by=admin.id,
            comment=data.comment if data else None,
        )
    except market_service.ListingStateError as e:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="LISTING_ALREADY_DECIDED") from e
    listing = await _get_listing(session, listing.id)
    return SkillListingRead(**(await market_service.annotate_listings(session, [listing]))[0])


@router.delete("/market/skills/{listing_id}", response_model=SkillListingRead)
async def delist_listing(
    listing_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> SkillListingRead:
    listing = await _get_listing(session, listing_id)
    try:
        listing = await market_service.delist(
            session, listing, user_id=user.id, is_admin=user.role == "admin"
        )
    except market_service.NotOwnerError as e:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="NOT_LISTING_OWNER") from e
    listing = await _get_listing(session, listing.id)
    return SkillListingRead(**(await market_service.annotate_listings(session, [listing]))[0])


@router.get("/market/skills/{listing_id}/reviews", response_model=list[SkillRatingRead])
async def list_reviews(
    listing_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> list[SkillRatingRead]:
    await _get_listing(session, listing_id)
    ratings = await market_service.list_ratings(session, listing_id)
    return [SkillRatingRead.model_validate(r) for r in ratings]


@router.post(
    "/market/skills/{listing_id}/reviews",
    response_model=SkillRatingRead,
    status_code=status.HTTP_201_CREATED,
)
async def add_review(
    listing_id: uuid.UUID,
    data: SkillRatingCreate,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> SkillRatingRead:
    listing = await _get_listing(session, listing_id)
    try:
        rating = await market_service.upsert_rating(session, listing, user_id=user.id, data=data)
    except market_service.ListingStateError as e:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="LISTING_NOT_APPROVED") from e
    return SkillRatingRead.model_validate(rating)
