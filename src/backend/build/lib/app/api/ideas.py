"""Idea Forge / 评审锦标赛 / 讨论路由（docs/api-m3.md）。

权限：一律项目成员（非成员 404 不泄露存在性）；promote 仅项目 owner / 平台 admin。
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import current_active_user
from app.core.db import get_session
from app.core.events import EventBus, get_event_bus
from app.core.queue import TaskQueue, get_task_queue
from app.models.idea import Idea
from app.models.project import Project
from app.models.review import ReviewSession
from app.models.user import User
from app.schemas.gate import GateRead
from app.schemas.idea import (
    ForgeRequest,
    ForgeStateRead,
    IdeaDetail,
    IdeaLeaderboardEntry,
    IdeaRead,
    IdeaUpdate,
)
from app.schemas.review import (
    ReviewMessageCreate,
    ReviewMessageRead,
    ReviewSessionRead,
    TournamentRequest,
)
from app.schemas.voyage import VoyageRead
from app.services import ideas as ideas_service
from app.services import projects as projects_service
from app.services import review as review_service

router = APIRouter(tags=["ideas"])


async def _member_project(session: AsyncSession, project_id: uuid.UUID, user: User) -> Project:
    project = await projects_service.get_project(session, project_id=project_id, user_id=user.id)
    if project is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="PROJECT_NOT_FOUND")
    return project


async def _member_idea(session: AsyncSession, idea_id: uuid.UUID, user: User) -> Idea:
    idea = await ideas_service.get_idea_for_user(session, idea_id=idea_id, user_id=user.id)
    if idea is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="IDEA_NOT_FOUND")
    return idea


async def _member_session(
    session: AsyncSession, session_id: uuid.UUID, user: User
) -> ReviewSession:
    review_session = await session.get(ReviewSession, session_id)
    if review_session is not None:
        project_id = await review_service.session_project_id(session, review_session)
        if project_id is not None and await projects_service.get_project(
            session, project_id=project_id, user_id=user.id
        ):
            return review_session
    raise HTTPException(status.HTTP_404_NOT_FOUND, detail="SESSION_NOT_FOUND")


# ---- Idea Forge（生成） ----


@router.post(
    "/projects/{project_id}/forge",
    response_model=VoyageRead,
    status_code=status.HTTP_201_CREATED,
)
async def start_forge(
    project_id: uuid.UUID,
    data: ForgeRequest,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
    queue: TaskQueue = Depends(get_task_queue),
) -> VoyageRead:
    project = await _member_project(session, project_id, user)
    try:
        run = await ideas_service.create_forge_voyage(
            session, project=project, knobs=data.knobs, created_by=user.id
        )
    except ideas_service.IdeaVoyageConflictError as e:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="IDEA_VOYAGE_ALREADY_RUNNING") from e
    await queue.enqueue("run_voyage", str(run.id))
    return VoyageRead.model_validate(run)


@router.get("/projects/{project_id}/forge/state", response_model=ForgeStateRead)
async def get_forge_state(
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> ForgeStateRead:
    project = await _member_project(session, project_id, user)
    state = await ideas_service.forge_state(session, project)
    return ForgeStateRead(**state)


# ---- Ideas ----


@router.get("/projects/{project_id}/ideas", response_model=list[IdeaRead])
async def list_ideas(
    project_id: uuid.UUID,
    status_filter: str | None = Query(default=None, alias="status"),
    sort: str = Query(default="-created_at", pattern="^(elo|-created_at|score)$"),
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> list[IdeaRead]:
    await _member_project(session, project_id, user)
    ideas = await ideas_service.list_ideas(
        session, project_id=project_id, status=status_filter, sort=sort
    )
    return [IdeaRead.model_validate(i) for i in ideas]


@router.get("/ideas/{idea_id}", response_model=IdeaDetail)
async def get_idea(
    idea_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> IdeaDetail:
    idea = await _member_idea(session, idea_id, user)
    parents = await ideas_service.parent_papers_brief(session, idea)
    return IdeaDetail(
        **IdeaRead.model_validate(idea).model_dump(),
        content=idea.content,
        parent_paper_ids=ideas_service.parse_parent_ids(idea),
        parent_papers=parents,
        score_rationale=idea.score_rationale,
    )


@router.patch("/ideas/{idea_id}", response_model=IdeaRead)
async def update_idea(
    idea_id: uuid.UUID,
    data: IdeaUpdate,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
    bus: EventBus = Depends(get_event_bus),
) -> IdeaRead:
    """人工淘汰（status=rejected）；其他状态转换走 promote/锦标赛专用接口。"""
    idea = await _member_idea(session, idea_id, user)
    idea = await ideas_service.set_idea_status(session, idea, data.status)
    await bus.publish_notify(
        idea.project_id,
        {"type": "idea.status", "idea_id": str(idea.id), "status": idea.status},
    )
    return IdeaRead.model_validate(idea)


@router.post(
    "/ideas/{idea_id}/promote", response_model=GateRead, status_code=status.HTTP_201_CREATED
)
async def promote_idea(
    idea_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
    bus: EventBus = Depends(get_event_bus),
) -> GateRead:
    """创建 idea_promotion 闸门（pending）；审批通过后 gates approve 联动置 promoted。"""
    idea = await _member_idea(session, idea_id, user)
    if not await ideas_service.can_promote(session, project_id=idea.project_id, user=user):
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="PROMOTE_FORBIDDEN")
    if idea.status == "promoted":
        raise HTTPException(status.HTTP_409_CONFLICT, detail="IDEA_ALREADY_PROMOTED")
    if await ideas_service.find_pending_promotion_gate(session, idea.id) is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="PROMOTION_ALREADY_PENDING")
    gate = await ideas_service.create_promotion_gate(session, idea, user)
    gate_read = GateRead.model_validate(gate)
    await bus.publish_notify(
        idea.project_id, {"type": "gate.created", "gate": gate_read.model_dump(mode="json")}
    )
    return gate_read


# ---- 评审锦标赛 ----


@router.post(
    "/projects/{project_id}/review/tournament",
    response_model=VoyageRead,
    status_code=status.HTTP_201_CREATED,
)
async def start_tournament(
    project_id: uuid.UUID,
    data: TournamentRequest,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
    queue: TaskQueue = Depends(get_task_queue),
) -> VoyageRead:
    project = await _member_project(session, project_id, user)
    try:
        run = await ideas_service.create_tournament_voyage(
            session, project=project, data=data, created_by=user.id
        )
    except ideas_service.IdeaVoyageConflictError as e:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="IDEA_VOYAGE_ALREADY_RUNNING") from e
    except ideas_service.InvalidIdeaIdsError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="INVALID_IDEA_IDS") from e
    except ideas_service.NotEnoughIdeasError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="NOT_ENOUGH_IDEAS") from e
    await queue.enqueue("run_voyage", str(run.id))
    return VoyageRead.model_validate(run)


@router.get("/projects/{project_id}/review/leaderboard", response_model=list[IdeaLeaderboardEntry])
async def get_leaderboard(
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> list[IdeaLeaderboardEntry]:
    await _member_project(session, project_id, user)
    ideas = await ideas_service.list_ideas(session, project_id=project_id, sort="elo")
    return [IdeaLeaderboardEntry.model_validate(i) for i in ideas]


# ---- 讨论（人机同场） ----


@router.get("/ideas/{idea_id}/sessions", response_model=list[ReviewSessionRead])
async def list_idea_sessions(
    idea_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> list[ReviewSessionRead]:
    idea = await _member_idea(session, idea_id, user)
    await review_service.get_or_create_discussion(session, idea.id)  # 惰性创建讨论区
    sessions = await review_service.list_idea_sessions(session, idea.id)
    return [ReviewSessionRead.model_validate(s) for s in sessions]


@router.get("/sessions/{session_id}/messages", response_model=list[ReviewMessageRead])
async def list_session_messages(
    session_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> list[ReviewMessageRead]:
    review_session = await _member_session(session, session_id, user)
    messages = await review_service.list_messages(session, review_session.id)
    return [ReviewMessageRead.model_validate(m) for m in messages]


@router.post(
    "/sessions/{session_id}/messages",
    response_model=ReviewMessageRead,
    status_code=status.HTTP_201_CREATED,
)
async def post_session_message(
    session_id: uuid.UUID,
    data: ReviewMessageCreate,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
    bus: EventBus = Depends(get_event_bus),
) -> ReviewMessageRead:
    review_session = await _member_session(session, session_id, user)
    project_id = await review_service.session_project_id(session, review_session)
    message = await review_service.add_human_message(session, review_session, user, data.content)
    message_read = ReviewMessageRead.model_validate(message)
    if project_id is not None:
        await bus.publish_notify(
            project_id,
            {
                "type": "review.message",
                "session_id": str(review_session.id),
                "project_id": str(project_id),
                "message": message_read.model_dump(mode="json"),
            },
        )
    return message_read
