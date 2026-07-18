"""论文分享 PPT 路由（文献追踪板块）：发起生成任务 / 下载产物。

- POST /projects/{pid}/presentations：single（单篇分享）| survey（多篇梳理）→
  创建 kind=presentation 的 AI 任务并入队；
- GET /presentations/{voyage_id}/file：下载生成的 .pptx（项目成员）。
"""

import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import current_active_user, require_llm_task
from app.core.db import get_session
from app.core.queue import TaskQueue, get_task_queue
from app.models.paper import Paper
from app.models.user import User
from app.models.voyage import VoyageRun
from app.schemas.voyage import VoyageRead
from app.services import gates as gates_service
from app.services import voyages as voyages_service

router = APIRouter(tags=["presentations"])


class PresentationCreate(BaseModel):
    paper_ids: list[uuid.UUID] = Field(min_length=1, max_length=12)
    mode: str = Field(default="single", pattern="^(single|survey)$")
    notes: str | None = Field(default=None, max_length=2000)  # 讲者侧重点


@router.post(
    "/projects/{project_id}/presentations",
    response_model=VoyageRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_presentation(
    project_id: uuid.UUID,
    data: PresentationCreate,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_llm_task),
    queue: TaskQueue = Depends(get_task_queue),
) -> VoyageRead:
    if not await gates_service.is_project_member(session, project_id, user.id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="PROJECT_NOT_FOUND")
    if data.mode == "single" and len(data.paper_ids) != 1:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail="SINGLE_NEEDS_ONE_PAPER")
    stmt = select(Paper.id, Paper.title).where(
        Paper.id.in_(data.paper_ids), Paper.project_id == project_id
    )
    rows = {pid: title for pid, title in (await session.execute(stmt)).all()}
    missing = [str(i) for i in data.paper_ids if i not in rows]
    if missing:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"PAPER_NOT_FOUND: {missing[0]}")

    first_title = rows[data.paper_ids[0]]
    goal = (
        f"论文分享 PPT：{first_title[:80]}"
        if data.mode == "single"
        else f"主题梳理 PPT：{first_title[:50]} 等 {len(data.paper_ids)} 篇"
    )
    run = VoyageRun(
        kind="presentation",
        goal=goal,
        status="planning",
        cursor=0,
        checkpoint={
            "params": {
                "paper_ids": [str(i) for i in data.paper_ids],
                "mode": data.mode,
                "notes": data.notes,
            }
        },
        project_id=project_id,
        created_by=user.id,
    )
    session.add(run)
    await session.commit()
    await session.refresh(run)
    await queue.enqueue("run_voyage", str(run.id))
    return VoyageRead.model_validate(run)


@router.get("/presentations/{voyage_id}/file")
async def download_presentation(
    voyage_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> FileResponse:
    run = await voyages_service.get_voyage(session, voyage_id=voyage_id, user_id=user.id)
    if run is None or run.kind != "presentation":
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="PRESENTATION_NOT_FOUND")
    info = (run.checkpoint or {}).get("presentation") or {}
    path = Path(str(info.get("path") or ""))
    if not path.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="FILE_NOT_READY")
    filename = f"{(info.get('deck_title') or 'presentation')[:60]}.pptx"
    return FileResponse(
        path,
        media_type="application/vnd.openxmlformats-officedocument.presentationml.presentation",
        filename=filename,
    )
