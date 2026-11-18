"""研究：输入一个方向 → 检索文献 → 寻找假设 → 想法出现在课题里，等用户选定。

推进逻辑在 services/research_runs.py；这里只做请求校验、鉴权和入队。创建接口立刻
返回（建好课题和记录就回），确定范围、检索、找假设都在后台进行，页面轮询详情接口。
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.voyage.failures import classify_failure
from app.api.auth import current_active_user
from app.core.db import get_session
from app.core.llm.errors import LLMError
from app.core.queue import TaskQueue, get_task_queue
from app.models.research_run import ResearchRun
from app.models.user import User
from app.schemas.research import (
    BriefRequest,
    MoreIdeasRequest,
    ResearchBrief,
    ResearchCreate,
    ResearchRename,
    ResearchRetry,
    ResearchRunDetail,
    ResearchRunRead,
)
from app.services import research_runs as research_service
from app.services import voyages as voyages_service

router = APIRouter(prefix="/research", tags=["research"])


async def _owned(session: AsyncSession, run_id: uuid.UUID, user: User) -> ResearchRun:
    run = await research_service.get_owned_run(session, run_id, user.id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="RESEARCH_RUN_NOT_FOUND")
    return run


def _direction(raw: str) -> str:
    """方向原文：去掉首尾空白后 1..2000 个字，否则 422 DIRECTION_INVALID。"""
    text = (raw or "").strip()
    if not 1 <= len(text) <= 2000:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail="DIRECTION_INVALID")
    return text


# 模型这一侧的失败用 424：502–504 在界面上表示「连不上本机引擎」，不能混用
_LLM_FAILED = status.HTTP_424_FAILED_DEPENDENCY


async def _kick(queue: TaskQueue, run_id: uuid.UUID) -> None:
    await queue.enqueue("research_advance", str(run_id), _job_id=f"research-{run_id}")


@router.post("/brief", response_model=ResearchBrief)
async def draft_brief(
    data: BriefRequest,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> ResearchBrief:
    """从一句研究方向推出检索简报，供「调整范围」预览和编辑。不创建任何东西。"""
    direction = _direction(data.direction)
    try:
        return await research_service.derive_brief(session, direction, user_id=user.id)
    except LLMError as e:
        found = classify_failure(e)
        if found.code in research_service.AUTH_CODES:
            raise HTTPException(_LLM_FAILED, detail="LLM_AUTH") from e
        code = "LLM_UNREACHABLE" if found.transient else "PLAN_FAILED"
        raise HTTPException(_LLM_FAILED, detail=code) from e
    except ValueError as e:  # 简报里没有可用的检索关键词
        raise HTTPException(_LLM_FAILED, detail="PLAN_FAILED") from e


@router.post("", response_model=ResearchRunRead, status_code=status.HTTP_201_CREATED)
async def create_research(
    data: ResearchCreate,
    response: Response,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
    queue: TaskQueue = Depends(get_task_queue),
) -> ResearchRunRead:
    """开始一次研究。新建回 201；同一个 client_key（或 60 秒内同样的方向）回 200 和
    原来那一条。"""
    run, created = await research_service.create_run(
        session,
        user_id=user.id,
        direction=_direction(data.direction),
        client_key=data.client_key,
        brief=data.brief,
    )
    if created:
        await _kick(queue, run.id)
    else:
        response.status_code = status.HTTP_200_OK
    return await research_service.to_read(session, run)


@router.get("", response_model=list[ResearchRunRead])
async def list_research(
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> list[ResearchRunRead]:
    runs = await research_service.list_runs(session, user.id)
    return [await research_service.to_read(session, run) for run in runs]


@router.get("/{run_id}", response_model=ResearchRunDetail)
async def get_research(
    run_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> ResearchRunDetail:
    run = await _owned(session, run_id, user)
    return await research_service.to_detail(session, run, user_id=user.id)


@router.post("/{run_id}/retry", response_model=ResearchRunRead)
async def retry_research(
    run_id: uuid.UUID,
    data: ResearchRetry | None = None,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
    queue: TaskQueue = Depends(get_task_queue),
) -> ResearchRunRead:
    """重试当前阶段；带改过的简报就按它重新检索。"""
    run = await _owned(session, run_id, user)
    data = data or ResearchRetry()
    try:
        run = await research_service.retry_run(
            session, run, queue.enqueue, brief=data.brief, phase=data.phase, broaden=data.broaden
        )
    except research_service.ResearchError as e:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=e.code) from e
    except voyages_service.LibraryTooSmallError as e:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="LIBRARY_TOO_SMALL") from e
    except ValueError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail="BRIEF_INVALID") from e
    return await research_service.to_read(session, run)


@router.post("/{run_id}/cancel", response_model=ResearchRunRead)
async def cancel_research(
    run_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> ResearchRunRead:
    """停止这次研究。已找到的论文和想法都保留。"""
    run = await _owned(session, run_id, user)
    try:
        run = await research_service.cancel_run(session, run)
    except research_service.ResearchError as e:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=e.code) from e
    return await research_service.to_read(session, run)


@router.post("/{run_id}/more-ideas", response_model=ResearchRunRead)
async def more_ideas(
    run_id: uuid.UUID,
    data: MoreIdeasRequest | None = None,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
    queue: TaskQueue = Depends(get_task_queue),
) -> ResearchRunRead:
    """在同一个文献库上再找一批假设。"""
    run = await _owned(session, run_id, user)
    try:
        run = await research_service.more_ideas(
            session, run, queue.enqueue, rounds=(data.rounds if data else None)
        )
    except research_service.ResearchError as e:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=e.code) from e
    except voyages_service.LibraryTooSmallError as e:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="LIBRARY_TOO_SMALL") from e
    return await research_service.to_read(session, run)


@router.delete("/{run_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_research(
    run_id: uuid.UUID,
    purge: bool = False,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> None:
    """删掉这次研究的记录。课题、文献库和想法默认保留；``?purge=true`` 一并删除。"""
    run = await _owned(session, run_id, user)
    await research_service.delete_run(session, run, purge=purge)


@router.patch("/{run_id}", response_model=ResearchRunRead)
async def rename_research(
    run_id: uuid.UUID,
    data: ResearchRename,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> ResearchRunRead:
    """改名：课题和它的文献库一起改。"""
    run = await _owned(session, run_id, user)
    run = await research_service.rename_run(session, run, data.name)
    return await research_service.to_read(session, run)
