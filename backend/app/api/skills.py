"""技能路由（docs/skill-system.md §4.1/§4.2）。

- /skills：技能 CRUD / 版本 / fork（builtin 只读，user 技能仅本人可改）
- /projects/{pid}/skills + /project-skills/{id}：启用到项目（项目成员）
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import current_active_user
from app.core.db import get_session
from app.core.queue import TaskQueue, get_task_queue
from app.models.skill import Skill
from app.models.user import User
from app.schemas.skill import (
    ProjectSkillCreate,
    ProjectSkillRead,
    ProjectSkillUpdate,
    SkillCreate,
    SkillDetail,
    SkillRead,
    SkillRunRequest,
    SkillTestRequest,
    SkillTestResult,
    SkillVersionCreate,
    SkillVersionRead,
)
from app.schemas.voyage import VoyageRead
from app.services import gates as gates_service
from app.services import skills as skills_service

router = APIRouter(tags=["skills"])


async def _get_visible_skill(session: AsyncSession, skill_id: uuid.UUID, user: User) -> Skill:
    skill = await skills_service.get_skill(session, skill_id, user_id=user.id)
    if skill is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="SKILL_NOT_FOUND")
    return skill


async def _require_member(session: AsyncSession, project_id: uuid.UUID, user: User) -> None:
    if not await gates_service.is_project_member(session, project_id, user.id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="PROJECT_NOT_FOUND")


# ---- 技能库 ----


@router.get("/skills", response_model=list[SkillRead])
async def list_skills(
    scope: str | None = Query(default=None, pattern="^(builtin|mine)$"),
    kind: str | None = Query(default=None),
    q: str | None = Query(default=None, max_length=100),
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> list[SkillRead]:
    skills = await skills_service.list_skills(session, user_id=user.id, scope=scope, kind=kind, q=q)
    return [SkillRead.model_validate(s) for s in skills]


@router.post("/skills", response_model=SkillDetail, status_code=status.HTTP_201_CREATED)
async def create_skill(
    data: SkillCreate,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> SkillDetail:
    try:
        skill = await skills_service.create_skill(session, owner_id=user.id, data=data)
    except skills_service.SkillSlugConflictError as e:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="SKILL_SLUG_TAKEN") from e
    except skills_service.SkillWorkflowInvalidError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e)) from e
    return await _detail(session, skill)


async def _detail(session: AsyncSession, skill: Skill) -> SkillDetail:
    version = await skills_service.latest_version(session, skill.id)
    detail = SkillDetail.model_validate(skill)
    detail.current_version = SkillVersionRead.model_validate(version) if version else None
    return detail


@router.get("/skills/{skill_id}", response_model=SkillDetail)
async def get_skill(
    skill_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> SkillDetail:
    skill = await _get_visible_skill(session, skill_id, user)
    return await _detail(session, skill)


@router.get("/skills/{skill_id}/versions", response_model=list[SkillVersionRead])
async def list_skill_versions(
    skill_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> list[SkillVersionRead]:
    skill = await _get_visible_skill(session, skill_id, user)
    versions = await skills_service.list_versions(session, skill.id)
    return [SkillVersionRead.model_validate(v) for v in versions]


@router.post(
    "/skills/{skill_id}/versions",
    response_model=SkillVersionRead,
    status_code=status.HTTP_201_CREATED,
)
async def add_skill_version(
    skill_id: uuid.UUID,
    data: SkillVersionCreate,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> SkillVersionRead:
    skill = await _get_visible_skill(session, skill_id, user)
    try:
        version = await skills_service.add_version(session, skill, user_id=user.id, data=data)
    except skills_service.SkillReadOnlyError as e:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="SKILL_READ_ONLY") from e
    except skills_service.SkillWorkflowInvalidError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e)) from e
    return SkillVersionRead.model_validate(version)


@router.post("/skills/{skill_id}/fork", response_model=SkillDetail, status_code=201)
async def fork_skill(
    skill_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> SkillDetail:
    skill = await _get_visible_skill(session, skill_id, user)
    fork = await skills_service.fork_skill(session, skill, user_id=user.id)
    return await _detail(session, fork)


@router.post("/skills/{skill_id}/test", response_model=SkillTestResult)
async def test_skill(
    skill_id: uuid.UUID,
    data: SkillTestRequest | None = None,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> SkillTestResult:
    """试运行：预览注入文本；guidance/rubric 真实调用一次 LLM（stage=default）。"""
    skill = await _get_visible_skill(session, skill_id, user)
    data = data or SkillTestRequest()
    result = await skills_service.test_run_skill(
        session, skill, user_id=user.id, goal=data.goal, target=data.target
    )
    return SkillTestResult(**result)


@router.post("/skills/{skill_id}/run", response_model=VoyageRead, status_code=201)
async def run_workflow_skill(
    skill_id: uuid.UUID,
    data: SkillRunRequest,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
    queue: TaskQueue = Depends(get_task_queue),
) -> VoyageRead:
    """「运行此流程」：以 workflow 技能 steps 为计划创建 AI 任务并入队执行。"""
    skill = await _get_visible_skill(session, skill_id, user)
    await _require_member(session, data.project_id, user)
    try:
        run = await skills_service.run_workflow_skill(
            session,
            skill,
            project_id=data.project_id,
            created_by=user.id,
            goal=data.goal,
            run_vars=data.vars,
            budget=data.budget,
        )
    except skills_service.SkillWorkflowInvalidError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e)) from e
    await queue.enqueue("run_voyage", str(run.id))
    return VoyageRead.model_validate(run)


@router.delete("/skills/{skill_id}", status_code=status.HTTP_204_NO_CONTENT)
async def archive_skill(
    skill_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> None:
    skill = await _get_visible_skill(session, skill_id, user)
    try:
        await skills_service.archive_skill(session, skill, user_id=user.id)
    except skills_service.SkillReadOnlyError as e:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="SKILL_READ_ONLY") from e


# ---- 启用到项目 ----


def _enable_read(row) -> ProjectSkillRead:  # noqa: ANN001 — ProjectSkill（惰性关系已加载）
    read = ProjectSkillRead.model_validate(row)
    if row.skill is not None:
        read.skill = SkillRead.model_validate(row.skill)
    return read


@router.get("/projects/{project_id}/skills", response_model=list[ProjectSkillRead])
async def list_project_skills(
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> list[ProjectSkillRead]:
    await _require_member(session, project_id, user)
    rows = await skills_service.list_project_skills(session, project_id)
    return [_enable_read(r) for r in rows]


@router.post(
    "/projects/{project_id}/skills",
    response_model=ProjectSkillRead,
    status_code=status.HTTP_201_CREATED,
)
async def enable_project_skill(
    project_id: uuid.UUID,
    data: ProjectSkillCreate,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> ProjectSkillRead:
    await _require_member(session, project_id, user)
    skill = await _get_visible_skill(session, data.skill_id, user)
    try:
        row = await skills_service.enable_skill(
            session, project_id=project_id, user_id=user.id, data=data, skill=skill
        )
    except skills_service.SkillWorkflowInvalidError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e)) from e
    except IntegrityError as e:
        await session.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, detail="SKILL_ALREADY_ENABLED") from e
    row = await skills_service.get_project_skill(session, row.id)
    assert row is not None
    return _enable_read(row)


async def _get_member_enable_row(session: AsyncSession, enable_id: uuid.UUID, user: User):
    row = await skills_service.get_project_skill(session, enable_id)
    if row is None or not await gates_service.is_project_member(session, row.project_id, user.id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="PROJECT_SKILL_NOT_FOUND")
    return row


@router.patch("/project-skills/{enable_id}", response_model=ProjectSkillRead)
async def update_project_skill(
    enable_id: uuid.UUID,
    data: ProjectSkillUpdate,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> ProjectSkillRead:
    row = await _get_member_enable_row(session, enable_id, user)
    row = await skills_service.update_project_skill(session, row, data)
    return _enable_read(row)


@router.delete("/project-skills/{enable_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_project_skill(
    enable_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> None:
    row = await _get_member_enable_row(session, enable_id, user)
    await skills_service.delete_project_skill(session, row)
