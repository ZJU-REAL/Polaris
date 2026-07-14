"""Experiment Lab 路由（docs/api-m4.md §2/§4）。

权限：一律项目成员（非成员 404 不泄露存在性）；凭据校验属当前用户。
日志读取只走本地镜像文件（worker 轮询远端时同步写入），不在请求线程碰 SSH。
"""

import asyncio
import json
import time
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import current_active_user
from app.core.db import get_session, get_sessionmaker
from app.core.events import EventBus, get_event_bus
from app.core.queue import TaskQueue, get_task_queue
from app.models.experiment import EXPERIMENT_TERMINAL_STATUSES, Experiment
from app.models.user import User
from app.schemas.experiment import (
    ExperimentCreate,
    ExperimentDetail,
    ExperimentLogsRead,
    ExperimentRead,
)
from app.services import experiments as experiments_service
from app.services import projects as projects_service

router = APIRouter(tags=["experiments"])

_HEARTBEAT_SECONDS = 15.0
_STREAM_POLL_SECONDS = 1.0
_STREAM_INITIAL_TAIL = 200


async def _member_experiment(
    session: AsyncSession, experiment_id: uuid.UUID, user: User
) -> tuple[Experiment, str]:
    row = await experiments_service.get_experiment_for_user(
        session, experiment_id=experiment_id, user_id=user.id
    )
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="EXPERIMENT_NOT_FOUND")
    return row


@router.post(
    "/projects/{project_id}/experiments",
    response_model=ExperimentRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_experiment(
    project_id: uuid.UUID,
    data: ExperimentCreate,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
    queue: TaskQueue = Depends(get_task_queue),
) -> ExperimentRead:
    project = await projects_service.get_project(session, project_id=project_id, user_id=user.id)
    if project is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="PROJECT_NOT_FOUND")
    try:
        experiment, voyage, idea_title = await experiments_service.create_experiment(
            session, project=project, data=data, user_id=user.id
        )
    except experiments_service.IdeaNotFoundError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="IDEA_NOT_FOUND") from e
    except experiments_service.IdeaNotPromotedError as e:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="IDEA_NOT_PROMOTED") from e
    except experiments_service.CredentialNotFoundError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="CREDENTIAL_NOT_FOUND") from e
    await queue.enqueue("run_voyage", str(voyage.id))
    return experiments_service.to_read(experiment, idea_title)


@router.get("/projects/{project_id}/experiments", response_model=list[ExperimentRead])
async def list_experiments(
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> list[ExperimentRead]:
    project = await projects_service.get_project(session, project_id=project_id, user_id=user.id)
    if project is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="PROJECT_NOT_FOUND")
    rows = await experiments_service.list_experiments(session, project_id=project_id)
    return [experiments_service.to_read(exp, title) for exp, title in rows]


@router.get("/experiments/{experiment_id}", response_model=ExperimentDetail)
async def get_experiment(
    experiment_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> ExperimentDetail:
    experiment, idea_title = await _member_experiment(session, experiment_id, user)
    base = experiments_service.to_read(experiment, idea_title)
    return ExperimentDetail(
        **base.model_dump(),
        plan=experiment.plan,
        runs=experiments_service.serialize_runs(experiment),
        report=experiment.report,
        metrics=experiment.metrics,
        figures=experiments_service.serialize_figures(experiment),
        iteration_state=experiment.iteration_state,
    )


@router.get("/experiments/{experiment_id}/figures/{index}/image")
async def get_experiment_figure_image(
    experiment_id: uuid.UUID,
    index: int,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> FileResponse:
    """实验图表 PNG（成员校验，模式同论文 figures 图片端点，docs/api-m5-a.md §3）。"""
    experiment, _ = await _member_experiment(session, experiment_id, user)
    figure = next((f for f in experiment.figures or [] if int(f.get("index", -1)) == index), None)
    path = Path(str(figure["path"])) if figure and figure.get("path") else None
    if figure is None or path is None or not path.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="FIGURE_NOT_FOUND")
    return FileResponse(
        path,
        media_type="image/png",
        filename=str(figure.get("name") or f"fig_{index}.png"),
        content_disposition_type="inline",
    )


@router.post("/experiments/{experiment_id}/cancel", response_model=ExperimentRead)
async def cancel_experiment(
    experiment_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
    bus: EventBus = Depends(get_event_bus),
) -> ExperimentRead:
    experiment, idea_title = await _member_experiment(session, experiment_id, user)
    try:
        experiment = await experiments_service.cancel_experiment(session, experiment)
    except experiments_service.ExperimentAlreadyFinishedError as e:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="EXPERIMENT_ALREADY_FINISHED") from e
    await bus.publish_notify(
        experiment.project_id,
        {
            "type": "experiment.status",
            "experiment_id": str(experiment.id),
            "status": experiment.status,
        },
    )
    if experiment.voyage_id is not None:
        await bus.publish_notify(
            experiment.project_id,
            {
                "type": "voyage.status",
                "voyage_id": str(experiment.voyage_id),
                "status": "cancelled",
            },
        )
    return experiments_service.to_read(experiment, idea_title)


def _resolve_log_path(experiment: Experiment, run_id: uuid.UUID | None) -> str | None:
    if run_id is not None:
        run = next((r for r in experiment.runs if r.id == run_id), None)
        if run is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="RUN_NOT_FOUND")
        return run.log_path
    run = experiments_service.latest_run(experiment)
    return run.log_path if run else None


@router.get("/experiments/{experiment_id}/logs", response_model=ExperimentLogsRead)
async def get_experiment_logs(
    experiment_id: uuid.UUID,
    run_id: uuid.UUID | None = Query(default=None),
    tail: int = Query(default=500, ge=1, le=5000),
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> ExperimentLogsRead:
    experiment, _ = await _member_experiment(session, experiment_id, user)
    log_path = _resolve_log_path(experiment, run_id)
    lines, truncated = experiments_service.read_local_log_tail(log_path, tail)
    return ExperimentLogsRead(lines=lines, truncated=truncated)


def _sse_frame(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"


@router.get("/experiments/{experiment_id}/logs/stream")
async def stream_experiment_logs(
    experiment_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> StreamingResponse:
    """SSE：轮询本地日志镜像的追加内容转发；15s 心跳；实验终态且无新增后收流。"""
    experiment, _ = await _member_experiment(session, experiment_id, user)
    exp_id = experiment.id

    async def _snapshot() -> tuple[str, str | None]:
        """当前实验状态 + 最新 run 的日志路径（每轮重查，运行中会产生新 run）。"""
        async with get_sessionmaker()() as s:
            row = await experiments_service.get_experiment_for_user(
                s, experiment_id=exp_id, user_id=user.id
            )
        if row is None:
            return "failed", None
        exp = row[0]
        run = experiments_service.latest_run(exp)
        return exp.status, run.log_path if run else None

    async def stream() -> AsyncIterator[str]:
        status_now, log_path = await _snapshot()
        yield _sse_frame("status", {"status": status_now})
        offset = 0
        if log_path:
            lines, _trunc = experiments_service.read_local_log_tail(log_path, _STREAM_INITIAL_TAIL)
            path = Path(log_path)
            offset = path.stat().st_size if path.is_file() else 0
            if lines:
                yield _sse_frame("log", {"lines": lines})
        last_ping = time.monotonic()
        try:
            while True:
                status_now, new_path = await _snapshot()
                if new_path != log_path:  # 新 run：从头跟踪新文件
                    log_path, offset = new_path, 0
                if log_path:
                    path = Path(log_path)
                    if path.is_file():
                        size = path.stat().st_size
                        if size > offset:
                            with path.open("r", encoding="utf-8", errors="replace") as f:
                                f.seek(offset)
                                chunk = f.read()
                            offset = size
                            yield _sse_frame("log", {"lines": chunk.splitlines()})
                if status_now in EXPERIMENT_TERMINAL_STATUSES:
                    yield _sse_frame("end", {"status": status_now})
                    return
                if time.monotonic() - last_ping >= _HEARTBEAT_SECONDS:
                    yield ": ping\n\n"
                    last_ping = time.monotonic()
                await asyncio.sleep(_STREAM_POLL_SECONDS)
        except asyncio.CancelledError:
            raise

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
