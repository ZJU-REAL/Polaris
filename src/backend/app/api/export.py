"""一键全量导出 API（#690）：入队 + 下载。

进度不在这里——事件走既有 /paper-tasks/{task_id}/events SSE（与 Zotero 导入
同口径），前端拿 task_id 后自行订阅。
"""

import re
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import FileResponse
from redis.asyncio import Redis

from app.api.auth import current_active_user
from app.core.queue import TaskQueue, get_task_queue
from app.core.redis import get_redis_dep
from app.models.user import User
from app.services import full_export as full_export_service
from app.services import paper_enrich as paper_enrich_service

router = APIRouter(prefix="/export", tags=["export"])

#: task_id 是 uuid4().hex；下载端点拿它拼文件路径，先掐死一切非法形态
_TASK_ID_RE = re.compile(r"^[0-9a-f]{32}$")


@router.post("/full", status_code=status.HTTP_202_ACCEPTED)
async def start_full_export(
    user: User = Depends(current_active_user),
    redis: Redis = Depends(get_redis_dep),
    queue: TaskQueue = Depends(get_task_queue),
) -> dict[str, str]:
    """把用户的全部数据打包成 zip（后台任务）；返回 task_id 供订阅进度。

    同一用户限并发 1：导出是全库扫描 + 拷 PDF 的重活，叠着跑只会互相拖慢，
    且产物一样——已有进行中的直接 409。
    """
    task_id = uuid.uuid4().hex
    try:
        # NX 锁即「进行中」判据：worker 结束（成败皆然）删 key，TTL 兜底防 worker 崩死
        acquired = await redis.set(
            full_export_service.export_active_key(str(user.id)),
            task_id,
            nx=True,
            ex=full_export_service.EXPORT_ACTIVE_TTL_SECONDS,
        )
    except Exception as e:  # noqa: BLE001 — redis 不可达时进度/归属都没法登记
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, detail="TASK_SERVICE_UNAVAILABLE"
        ) from e
    if not acquired:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="EXPORT_ALREADY_RUNNING")
    try:
        # 两把归属 key：paper-task 的供 SSE 鉴权（1h，与事件日志同寿），
        # full_export 的供下载鉴权（24h，给足下载窗口）
        await redis.setex(
            paper_enrich_service.paper_task_owner_key(task_id), 3600, str(user.id)
        )
        await redis.setex(
            full_export_service.export_owner_key(task_id),
            full_export_service.EXPORT_OWNER_TTL_SECONDS,
            str(user.id),
        )
        await queue.enqueue("full_export", task_id=task_id, user_id=str(user.id))
    except Exception as e:  # noqa: BLE001 — 入队失败必须放锁，否则 2h 内导不了
        await redis.delete(full_export_service.export_active_key(str(user.id)))
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, detail="TASK_SERVICE_UNAVAILABLE"
        ) from e
    return {"task_id": task_id}


@router.get("/full/{task_id}/download")
async def download_full_export(
    task_id: str,
    user: User = Depends(current_active_user),
    redis: Redis = Depends(get_redis_dep),
) -> FileResponse:
    """下载导出 zip（仅属主；未就绪/过期/他人任务一律 404，不泄露存在性）。"""
    if not _TASK_ID_RE.fullmatch(task_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="EXPORT_NOT_FOUND")
    owner = await redis.get(full_export_service.export_owner_key(task_id))
    if owner != str(user.id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="EXPORT_NOT_FOUND")
    path = full_export_service.export_zip_path(task_id)
    if not path.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="EXPORT_NOT_READY")
    # 文件名固定 ASCII：Content-Disposition 是 latin-1 头，中文名会当场炸（同库页导出）
    return FileResponse(
        path, media_type="application/zip", filename="polaris-full-export.zip"
    )
