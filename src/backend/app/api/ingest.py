"""文献 ingest 路由（docs/api-m2.md §4）：冷启动/增量入队 + 状态查询。"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import current_active_user, require_llm_task
from app.core.db import get_session
from app.core.queue import TaskQueue, get_task_queue
from app.models.user import User
from app.schemas.ingest import IngestRequest, IngestStateRead
from app.schemas.voyage import VoyageRead
from app.services import ingest as ingest_service
from app.services import libraries as libraries_service

router = APIRouter(tags=["ingest"])


@router.post(
    "/projects/{project_id}/ingest",
    response_model=VoyageRead,
    status_code=status.HTTP_201_CREATED,
)
async def start_ingest(
    project_id: uuid.UUID,
    data: IngestRequest,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_llm_task),
    queue: TaskQueue = Depends(get_task_queue),
) -> VoyageRead:
    project = await libraries_service.get_managed_project(session, project_id=project_id, user=user)
    if project is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="PROJECT_NOT_FOUND")
    try:
        run = await ingest_service.create_ingest_voyage(
            session, project=project, mode=data.mode, knobs=data.knobs, created_by=user.id
        )
    except ingest_service.IngestConflictError as e:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="INGEST_ALREADY_RUNNING") from e
    await queue.enqueue("run_voyage", str(run.id))
    return VoyageRead.model_validate(run)


@router.get("/projects/{project_id}/ingest/state", response_model=IngestStateRead)
async def get_ingest_state(
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> IngestStateRead:
    project = await libraries_service.get_managed_project(session, project_id=project_id, user=user)
    if project is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="PROJECT_NOT_FOUND")
    state = await ingest_service.ingest_state(session, project)
    return IngestStateRead(**state)
