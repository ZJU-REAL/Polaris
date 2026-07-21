"""论文撰写路由（docs/api-m5-b.md §1/§2/§3/§4/§5/§7）。

权限：一律项目成员（非成员 404 不泄露存在性）；DELETE 稿件仅 owner/admin。
编译为同步端点（tectonic 硬超时 120s）；实时协同走 /ws/manuscripts/{fid}（api/ws.py）。
"""

import asyncio
import json
import logging
import mimetypes
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from fastapi.responses import FileResponse, Response, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import (
    current_active_user,
    require_admin,
    require_paper_review,
    require_writer,
)
from app.core.db import get_session
from app.core.events import EventBus, get_event_bus
from app.core.llm.fake import estimate_tokens
from app.core.llm.router import get_llm_router
from app.core.queue import TaskQueue, get_task_queue
from app.models.manuscript import Manuscript, ManuscriptFile
from app.models.user import User
from app.schemas.gate import GateRead
from app.schemas.manuscript import (
    AddCollaborator,
    AssistRequest,
    BatchResult,
    CollaboratorRead,
    CompileResult,
    DraftRequest,
    FileVersionContent,
    FileVersionMeta,
    FolderCreate,
    ManuscriptBatchAction,
    ManuscriptCreate,
    ManuscriptDetail,
    ManuscriptFileBrief,
    ManuscriptFileContent,
    ManuscriptFileCreate,
    ManuscriptFileRename,
    ManuscriptRead,
    ManuscriptUpdate,
    ShareLink,
    ShareLinkCreate,
    TemplateDownloadProgress,
    TemplateInfo,
    TemplateSeedResult,
)
from app.schemas.review import PaperReviewRequest, PaperReviewSummary
from app.schemas.voyage import VoyageRead
from app.services import latex_compile, writing_assist
from app.services import manuscript_templates as templates_service
from app.services import manuscript_versions as manuscript_versions_service
from app.services import manuscripts as manuscripts_service
from app.services import paper_review as paper_review_service
from app.services import projects as projects_service
from app.services.crdt_rooms import get_crdt_rooms

logger = logging.getLogger(__name__)

router = APIRouter(tags=["manuscripts"])


# ---- 内部小件 ----


async def _member_manuscript(
    session: AsyncSession, manuscript_id: uuid.UUID, user: User, with_files: bool = False
) -> Manuscript:
    manuscript = await manuscripts_service.get_manuscript_for_user(
        session, manuscript_id=manuscript_id, user_id=user.id, with_files=with_files
    )
    if manuscript is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="MANUSCRIPT_NOT_FOUND")
    return manuscript


def _file_brief(file: ManuscriptFile) -> ManuscriptFileBrief:
    if file.is_binary:
        raw = manuscripts_service.read_binary_asset(file.manuscript_id, file.path)
        size = len(raw) if raw is not None else 0
    else:
        size = len(file.content.encode("utf-8"))
    return ManuscriptFileBrief(
        id=file.id,
        path=file.path,
        size=size,
        readonly=file.readonly,
        is_binary=file.is_binary,
        is_folder=file.is_folder,
        updated_at=file.updated_at,
    )


async def _detail(session: AsyncSession, manuscript: Manuscript) -> ManuscriptDetail:
    voyage = await manuscripts_service.find_active_writing_voyage(session, manuscript)
    files = sorted(manuscript.files, key=lambda f: f.path)
    return ManuscriptDetail(
        **ManuscriptRead.model_validate(manuscript).model_dump(),
        files=[_file_brief(f) for f in files],
        fact_pack=manuscript.fact_pack,
        latest_compile=(
            CompileResult.model_validate(manuscript.latest_compile)
            if manuscript.latest_compile
            else None
        ),
        writing_voyage_id=voyage.id if voyage else None,
    )


# ---- §1 Manuscripts ----


@router.get("/manuscripts/templates", response_model=list[TemplateInfo])
async def list_templates(
    project_id: uuid.UUID | None = None,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> list[TemplateInfo]:
    """内置 + 全平台模板；带 project_id 时并入该研究方向私有的上传模板。"""
    infos = await templates_service.list_all(session, project_id=project_id)
    return [TemplateInfo.model_validate(m) for m in infos]


@router.post(
    "/manuscripts/templates",
    response_model=TemplateInfo,
    status_code=status.HTTP_201_CREATED,
)
async def upload_template(
    file: UploadFile = File(...),
    name: str = Form(...),
    description: str | None = Form(None),
    engine: str = Form("tectonic"),
    page_limit: int | None = Form(None),
    project_id: uuid.UUID | None = Form(None),
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> TemplateInfo:
    """上传 zip 建模板。给 project_id → 该研究方向私有（需成员）；否则全平台（需管理员）。"""
    if project_id is not None:
        project = await projects_service.get_project(
            session, project_id=project_id, user_id=user.id
        )
        if project is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="PROJECT_NOT_FOUND")
        scope = "project"
    else:
        if user.role != "admin":
            raise HTTPException(status.HTTP_403_FORBIDDEN, detail="ADMIN_REQUIRED_FOR_GLOBAL")
        scope = "global"
    data = await file.read()
    try:
        tmpl = await templates_service.create_from_zip(
            session,
            name=name,
            zip_bytes=data,
            scope=scope,
            project_id=project_id,
            created_by=user.id,
            description=description,
            engine=engine,
            page_limit=page_limit,
        )
    except templates_service.TemplateError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e)) from e
    return TemplateInfo.model_validate(templates_service._db_info(tmpl))


@router.get("/manuscripts/templates/{template_id}/download")
async def download_template(
    template_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> Response:
    tmpl = await templates_service.get_db_template(session, str(template_id))
    if tmpl is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="TEMPLATE_NOT_FOUND")
    if tmpl.scope == "project" and tmpl.project_id is not None:
        project = await projects_service.get_project(
            session, project_id=tmpl.project_id, user_id=user.id
        )
        if project is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="TEMPLATE_NOT_FOUND")
    content = await asyncio.to_thread(templates_service.zip_bytes, tmpl)
    return Response(
        content=content,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{tmpl.key}.zip"'},
    )


@router.delete("/manuscripts/templates/{template_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_template(
    template_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> None:
    tmpl = await templates_service.get_db_template(session, str(template_id))
    if tmpl is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="TEMPLATE_NOT_FOUND")
    allowed = user.role == "admin" or tmpl.created_by == user.id
    if not allowed and tmpl.project_id is not None:
        project = await projects_service.get_project(
            session, project_id=tmpl.project_id, user_id=user.id
        )
        allowed = project is not None and projects_service.can_manage_project(project, user)
    if not allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="NOT_ALLOWED")
    await templates_service.delete_template(session, tmpl)


@router.post("/manuscripts/templates/seed", response_model=list[TemplateSeedResult])
async def seed_templates(
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_admin),
) -> list[TemplateSeedResult]:
    """拉取并入库官方模板（zjuthesis/ACL/ICLR/NeurIPS/ICML），幂等。仅管理员。"""
    results: list[TemplateSeedResult] = []
    for entry in templates_service.SEED_MANIFEST:
        try:
            tmpl = await templates_service.seed_one(session, entry, created_by=user.id)
            if tmpl is None:
                results.append(
                    TemplateSeedResult(key=entry["key"], name=entry["name"], status="skipped")
                )
            else:
                results.append(
                    TemplateSeedResult(key=entry["key"], name=entry["name"], status="seeded")
                )
        except Exception as e:  # noqa: BLE001 — 单个源失败不影响其余
            logger.warning("template seed failed: %s", entry["key"], exc_info=True)
            results.append(
                TemplateSeedResult(
                    key=entry["key"],
                    name=entry["name"],
                    status="failed",
                    detail=f"{type(e).__name__}: {e}"[:300],
                )
            )
    return results


def _progress_model(key: str) -> TemplateDownloadProgress:
    p = templates_service.get_progress(key) or {
        "key": key,
        "name": templates_service.MANIFEST_BY_KEY.get(key, {}).get("name", key),
        "phase": "pending",
        "percent": 0,
        "detail": "",
    }
    return TemplateDownloadProgress.model_validate(p)


@router.post("/manuscripts/templates/download/{key}", response_model=TemplateDownloadProgress)
async def start_template_download(
    key: str,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> TemplateDownloadProgress:
    """按需在后台下载一个官方模板（首次使用自动触发），幂等。进度走 SSE 端点。"""
    if key not in templates_service.MANIFEST_BY_KEY:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="UNKNOWN_TEMPLATE")
    # 已下载过：直接回 done（带真实模板 id）
    existing = await templates_service.get_db_template(session, key)
    if existing is not None:
        return TemplateDownloadProgress(
            key=key, name=existing.name, phase="done", percent=100, template_id=str(existing.id)
        )
    try:
        templates_service.spawn_download(key, created_by=user.id)
    except templates_service.TemplateError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(e)) from e
    return _progress_model(key)


@router.get("/manuscripts/templates/download/{key}/progress")
async def template_download_progress(
    key: str,
    user: User = Depends(current_active_user),
) -> StreamingResponse:
    """SSE 推送某官方模板的下载进度：progress* → done{template_id} / error{detail}。"""
    if key not in templates_service.MANIFEST_BY_KEY:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="UNKNOWN_TEMPLATE")

    async def event_stream() -> AsyncIterator[str]:
        last: str | None = None
        # 最长盯 5 分钟，避免连接悬挂
        for _ in range(600):
            p = templates_service.get_progress(key)
            if p is not None:
                snapshot = f"{p['phase']}:{p.get('percent', 0)}:{p.get('detail', '')}"
                if snapshot != last:
                    last = snapshot
                    yield _sse_frame("progress", p)
                if p["phase"] == "done":
                    yield _sse_frame("done", {"template_id": p.get("template_id")})
                    return
                if p["phase"] == "failed":
                    yield _sse_frame("error", {"detail": p.get("error") or "下载失败"})
                    return
            await asyncio.sleep(0.5)
        yield _sse_frame("error", {"detail": "进度超时"})

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post(
    "/projects/{project_id}/manuscripts",
    response_model=ManuscriptRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_manuscript(
    project_id: uuid.UUID,
    data: ManuscriptCreate,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> ManuscriptRead:
    project = await projects_service.get_project(session, project_id=project_id, user_id=user.id)
    if project is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="PROJECT_NOT_FOUND")
    try:
        manuscript = await manuscripts_service.create_manuscript(
            session, project=project, data=data, user_id=user.id
        )
    except manuscripts_service.TemplateNotFoundError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="TEMPLATE_NOT_FOUND") from e
    except manuscripts_service.IdeaNotFoundError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="IDEA_NOT_FOUND") from e
    except manuscripts_service.ExperimentNotFoundError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="EXPERIMENT_NOT_FOUND") from e
    return ManuscriptRead.model_validate(manuscript)


@router.get("/projects/{project_id}/manuscripts", response_model=list[ManuscriptRead])
async def list_manuscripts(
    project_id: uuid.UUID,
    trashed: bool = False,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> list[ManuscriptRead]:
    project = await projects_service.get_project(session, project_id=project_id, user_id=user.id)
    if project is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="PROJECT_NOT_FOUND")
    rows = await manuscripts_service.list_manuscripts(
        session, project_id=project_id, trashed=trashed
    )
    return [ManuscriptRead.model_validate(m) for m in rows]


@router.get("/manuscripts/{manuscript_id}", response_model=ManuscriptDetail)
async def get_manuscript(
    manuscript_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> ManuscriptDetail:
    manuscript = await _member_manuscript(session, manuscript_id, user, with_files=True)
    return await _detail(session, manuscript)


@router.patch("/manuscripts/{manuscript_id}", response_model=ManuscriptRead)
async def update_manuscript(
    manuscript_id: uuid.UUID,
    data: ManuscriptUpdate,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> ManuscriptRead:
    manuscript = await _member_manuscript(session, manuscript_id, user, with_files=True)
    changed = False
    if data.title is not None:
        manuscript.title = data.title
        changed = True
    if data.main_tex is not None:
        tex_files = {f.path for f in manuscript.files if f.path.lower().endswith(".tex")}
        if data.main_tex not in tex_files:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="MAIN_TEX_NOT_FOUND",
            )
        manuscript.main_tex = data.main_tex
        changed = True
    if data.engine is not None:
        manuscript.engine = data.engine
        changed = True
    if data.pinned is not None:
        manuscript.pinned_at = datetime.now(UTC) if data.pinned else None
        changed = True
    if changed:
        await session.commit()
        await session.refresh(manuscript)
    return ManuscriptRead.model_validate(manuscript)


async def _manage_manuscript_project(session: AsyncSession, manuscript: Manuscript, user: User):
    """稿件所属项目 + 管理权限校验（垃圾箱/删除用）。"""
    project = await projects_service.get_project(
        session, project_id=manuscript.project_id, user_id=user.id
    )
    if project is None or not projects_service.can_manage_project(project, user):
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="OWNER_OR_ADMIN_REQUIRED")
    return project


@router.delete("/manuscripts/{manuscript_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_manuscript(
    manuscript_id: uuid.UUID,
    permanent: bool = False,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> None:
    """默认移入垃圾箱（软删除）；permanent=true 永久删除。"""
    manuscript = await _member_manuscript(session, manuscript_id, user)
    await _manage_manuscript_project(session, manuscript, user)
    if permanent:
        await session.delete(manuscript)
        await session.commit()
    else:
        await manuscripts_service.trash_manuscripts(
            session, project_id=manuscript.project_id, ids=[manuscript.id]
        )


@router.post("/manuscripts/{manuscript_id}/restore", response_model=ManuscriptRead)
async def restore_manuscript(
    manuscript_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> ManuscriptRead:
    """从垃圾箱恢复。"""
    manuscript = await _member_manuscript(session, manuscript_id, user)
    await _manage_manuscript_project(session, manuscript, user)
    await manuscripts_service.restore_manuscripts(
        session, project_id=manuscript.project_id, ids=[manuscript.id]
    )
    await session.refresh(manuscript)
    return ManuscriptRead.model_validate(manuscript)


@router.post("/projects/{project_id}/manuscripts/batch", response_model=BatchResult)
async def batch_manuscripts(
    project_id: uuid.UUID,
    data: ManuscriptBatchAction,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> BatchResult:
    """批量：trash 移入垃圾箱 / restore 恢复 / delete 永久删除。仅项目管理者可操作。"""
    project = await projects_service.get_project(session, project_id=project_id, user_id=user.id)
    if project is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="PROJECT_NOT_FOUND")
    if not projects_service.can_manage_project(project, user):
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="OWNER_OR_ADMIN_REQUIRED")
    if data.action == "trash":
        n = await manuscripts_service.trash_manuscripts(
            session, project_id=project_id, ids=data.ids
        )
    elif data.action == "restore":
        n = await manuscripts_service.restore_manuscripts(
            session, project_id=project_id, ids=data.ids
        )
    else:  # delete（永久）
        n = await manuscripts_service.purge_manuscripts(
            session, project_id=project_id, ids=data.ids
        )
    return BatchResult(affected=n)


@router.post("/projects/{project_id}/manuscripts/trash/empty", response_model=BatchResult)
async def empty_manuscript_trash(
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> BatchResult:
    """清空垃圾箱：永久删除该项目所有已在垃圾箱的稿件。"""
    project = await projects_service.get_project(session, project_id=project_id, user_id=user.id)
    if project is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="PROJECT_NOT_FOUND")
    if not projects_service.can_manage_project(project, user):
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="OWNER_OR_ADMIN_REQUIRED")
    n = await manuscripts_service.purge_manuscripts(session, project_id=project_id, ids=None)
    return BatchResult(affected=n)


# ---- §1b 协作者 / 分享（稿件权限=项目成员，操作落到所属研究方向） ----


async def _manuscript_project(session: AsyncSession, manuscript: Manuscript, user: User):
    project = await projects_service.get_project(
        session, project_id=manuscript.project_id, user_id=user.id
    )
    if project is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="PROJECT_NOT_FOUND")
    return project


@router.get("/manuscripts/{manuscript_id}/collaborators", response_model=list[CollaboratorRead])
async def list_collaborators(
    manuscript_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> list[CollaboratorRead]:
    manuscript = await _member_manuscript(session, manuscript_id, user)
    project = await _manuscript_project(session, manuscript, user)
    rows = await projects_service.list_members_detailed(session, project)
    return [CollaboratorRead.model_validate(r) for r in rows]


@router.post(
    "/manuscripts/{manuscript_id}/collaborators",
    response_model=list[CollaboratorRead],
    status_code=status.HTTP_201_CREATED,
)
async def add_collaborator(
    manuscript_id: uuid.UUID,
    data: AddCollaborator,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> list[CollaboratorRead]:
    """把平台用户加入所属研究方向（即获得该稿件协同编辑权）。需 owner/管理员。"""
    manuscript = await _member_manuscript(session, manuscript_id, user)
    project = await _manuscript_project(session, manuscript, user)
    if not projects_service.can_manage_project(project, user):
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="OWNER_OR_ADMIN_REQUIRED")
    ok = await projects_service.add_member_by_id(
        session, project.id, user_id=data.user_id, role=data.role
    )
    if not ok:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="USER_NOT_FOUND")
    rows = await projects_service.list_members_detailed(session, project)
    return [CollaboratorRead.model_validate(r) for r in rows]


@router.delete(
    "/manuscripts/{manuscript_id}/collaborators/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def remove_collaborator(
    manuscript_id: uuid.UUID,
    user_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> None:
    manuscript = await _member_manuscript(session, manuscript_id, user)
    project = await _manuscript_project(session, manuscript, user)
    if not projects_service.can_manage_project(project, user):
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="OWNER_OR_ADMIN_REQUIRED")
    if project.owner_id == user_id:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="CANNOT_REMOVE_OWNER")
    await projects_service.remove_member(session, project.id, user_id=user_id)


@router.post(
    "/manuscripts/{manuscript_id}/share-link",
    response_model=ShareLink,
    status_code=status.HTTP_201_CREATED,
)
async def create_share_link(
    manuscript_id: uuid.UUID,
    data: ShareLinkCreate | None = None,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> ShareLink:
    """生成协同编辑分享链接（复用研究方向邀请）：平台用户打开 /join/{token}
    登录后加入即获协同编辑权。需 owner/管理员。"""
    manuscript = await _member_manuscript(session, manuscript_id, user)
    project = await _manuscript_project(session, manuscript, user)
    if not projects_service.can_manage_project(project, user):
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="OWNER_OR_ADMIN_REQUIRED")
    data = data or ShareLinkCreate()
    invite = await projects_service.create_invite(
        session,
        project_id=project.id,
        created_by=user.id,
        expires_days=data.expires_days,
        max_uses=data.max_uses,
    )
    return ShareLink(
        token=invite.token,
        join_path=f"/join/{invite.token}",
        expires_at=invite.expires_at,
        max_uses=invite.max_uses,
    )


# ---- §2 文件 ----


async def _member_file(
    session: AsyncSession, manuscript: Manuscript, file_id: uuid.UUID
) -> ManuscriptFile:
    file = next((f for f in manuscript.files if f.id == file_id), None)
    if file is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="FILE_NOT_FOUND")
    return file


@router.get("/manuscripts/{manuscript_id}/files/{file_id}", response_model=ManuscriptFileContent)
async def get_file(
    manuscript_id: uuid.UUID,
    file_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> ManuscriptFileContent:
    manuscript = await _member_manuscript(session, manuscript_id, user, with_files=True)
    file = await _member_file(session, manuscript, file_id)
    # 有活跃协同房间时以房间内容为准（防抖快照可能落后 2s）
    room_content = get_crdt_rooms().room_content(file.id)
    return ManuscriptFileContent(
        id=file.id,
        path=file.path,
        content=room_content if room_content is not None else file.content,
        readonly=file.readonly,
    )


@router.get("/manuscripts/{manuscript_id}/files/{file_id}/raw")
async def get_file_raw(
    manuscript_id: uuid.UUID,
    file_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> Response:
    """二进制文件原始字节（图片/PDF/字体预览与下载）。"""
    manuscript = await _member_manuscript(session, manuscript_id, user, with_files=True)
    file = await _member_file(session, manuscript, file_id)
    if not file.is_binary:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="NOT_BINARY")
    data = manuscripts_service.read_binary_asset(manuscript.id, file.path)
    if data is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="ASSET_MISSING")
    media = mimetypes.guess_type(file.path)[0] or "application/octet-stream"
    filename = file.path.rsplit("/", 1)[-1]
    return Response(
        content=data,
        media_type=media,
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )


@router.post(
    "/manuscripts/{manuscript_id}/files",
    response_model=ManuscriptFileBrief,
    status_code=status.HTTP_201_CREATED,
)
async def create_file(
    manuscript_id: uuid.UUID,
    data: ManuscriptFileCreate,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> ManuscriptFileBrief:
    manuscript = await _member_manuscript(session, manuscript_id, user)
    try:
        file = await manuscripts_service.create_file(
            session, manuscript=manuscript, path=data.path, content=data.content, user_id=user.id
        )
    except manuscripts_service.FilePathInvalidError as e:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="FILE_PATH_INVALID") from e
    return _file_brief(file)


@router.post(
    "/manuscripts/{manuscript_id}/folders",
    response_model=ManuscriptFileBrief,
    status_code=status.HTTP_201_CREATED,
)
async def create_folder(
    manuscript_id: uuid.UUID,
    data: FolderCreate,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> ManuscriptFileBrief:
    manuscript = await _member_manuscript(session, manuscript_id, user)
    try:
        folder = await manuscripts_service.create_folder(
            session, manuscript=manuscript, path=data.path, user_id=user.id
        )
    except manuscripts_service.FilePathInvalidError as e:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="FILE_PATH_INVALID") from e
    return _file_brief(folder)


@router.post(
    "/manuscripts/{manuscript_id}/files/upload",
    response_model=ManuscriptFileBrief,
    status_code=status.HTTP_201_CREATED,
)
async def upload_file(
    manuscript_id: uuid.UUID,
    file: UploadFile = File(...),
    path: str | None = Form(None),
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> ManuscriptFileBrief:
    """上传文件（文本或二进制）；path 缺省用文件名。文本可编辑，二进制只读落盘。"""
    manuscript = await _member_manuscript(session, manuscript_id, user)
    target = (path or file.filename or "upload.bin").strip()
    data = await file.read()
    try:
        created = await manuscripts_service.upload_file(
            session, manuscript=manuscript, path=target, data=data, user_id=user.id
        )
    except manuscripts_service.FilePathInvalidError as e:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="FILE_PATH_INVALID") from e
    return _file_brief(created)


@router.patch("/manuscripts/{manuscript_id}/files/{file_id}", response_model=ManuscriptFileBrief)
async def rename_file(
    manuscript_id: uuid.UUID,
    file_id: uuid.UUID,
    data: ManuscriptFileRename,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> ManuscriptFileBrief:
    manuscript = await _member_manuscript(session, manuscript_id, user, with_files=True)
    file = await _member_file(session, manuscript, file_id)
    try:
        file = await manuscripts_service.rename_file(
            session, file=file, path=data.path, user_id=user.id
        )
    except manuscripts_service.FileReadonlyError as e:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="FILE_READONLY") from e
    except manuscripts_service.FilePathInvalidError as e:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="FILE_PATH_INVALID") from e
    return _file_brief(file)


@router.delete(
    "/manuscripts/{manuscript_id}/files/{file_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def delete_file(
    manuscript_id: uuid.UUID,
    file_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> None:
    manuscript = await _member_manuscript(session, manuscript_id, user, with_files=True)
    file = await _member_file(session, manuscript, file_id)
    try:
        await manuscripts_service.delete_file(session, file=file)
    except manuscripts_service.FileReadonlyError as e:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="FILE_READONLY") from e


# ---- §2b 文件版本历史 ----


def _version_meta(v) -> FileVersionMeta:
    return FileVersionMeta(
        id=v.id,
        seq=v.seq,
        origin=v.origin,
        label=v.label,
        size=len(v.content.encode("utf-8")),
        created_by=v.created_by,
        created_at=v.created_at,
    )


@router.get(
    "/manuscripts/{manuscript_id}/files/{file_id}/versions",
    response_model=list[FileVersionMeta],
)
async def list_file_versions(
    manuscript_id: uuid.UUID,
    file_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> list[FileVersionMeta]:
    manuscript = await _member_manuscript(session, manuscript_id, user, with_files=True)
    file = await _member_file(session, manuscript, file_id)
    versions = await manuscript_versions_service.list_versions(session, file.id)
    return [_version_meta(v) for v in versions]


@router.get(
    "/manuscripts/{manuscript_id}/files/{file_id}/versions/{version_id}",
    response_model=FileVersionContent,
)
async def get_file_version(
    manuscript_id: uuid.UUID,
    file_id: uuid.UUID,
    version_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> FileVersionContent:
    manuscript = await _member_manuscript(session, manuscript_id, user, with_files=True)
    file = await _member_file(session, manuscript, file_id)
    version = await manuscript_versions_service.get_version(session, file.id, version_id)
    if version is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="VERSION_NOT_FOUND")
    return FileVersionContent(**_version_meta(version).model_dump(), content=version.content)


@router.post(
    "/manuscripts/{manuscript_id}/files/{file_id}/versions/{version_id}/restore",
    response_model=ManuscriptFileContent,
)
async def restore_file_version(
    manuscript_id: uuid.UUID,
    file_id: uuid.UUID,
    version_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> ManuscriptFileContent:
    """恢复到指定版本：先把当前内容备份为 pre_restore 快照，再整文件替换。

    有活跃协同房间时经 Y 事务替换（协同者实时可见），无房间直接写库。
    """
    manuscript = await _member_manuscript(session, manuscript_id, user, with_files=True)
    file = await _member_file(session, manuscript, file_id)
    if file.readonly:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="FILE_READONLY")
    version = await manuscript_versions_service.get_version(session, file.id, version_id)
    if version is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="VERSION_NOT_FOUND")

    rooms = get_crdt_rooms()
    current = rooms.room_content(file.id)
    current = current if current is not None else file.content
    await manuscript_versions_service.snapshot_file(
        session,
        file,
        origin="pre_restore",
        label=f"恢复 #{version.seq} 前备份",
        created_by=user.id,
        content=current,
    )
    restored = version.content
    via_room = await rooms.set_content(file.id, restored)
    if not via_room:
        file.content = restored
        file.updated_by = user.id
    await session.commit()
    return ManuscriptFileContent(
        id=file.id, path=file.path, content=restored, readonly=file.readonly
    )


# ---- §3 fact-pack ----


@router.post("/manuscripts/{manuscript_id}/fact-pack/refresh")
async def refresh_fact_pack(
    manuscript_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> dict:
    manuscript = await _member_manuscript(session, manuscript_id, user)
    return await manuscripts_service.refresh_fact_pack(session, manuscript)


# ---- §4 编译 ----


@router.post("/manuscripts/{manuscript_id}/compile", response_model=CompileResult)
async def compile_manuscript(
    manuscript_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> CompileResult:
    manuscript = await _member_manuscript(session, manuscript_id, user)
    result = await latex_compile.compile_manuscript(session, manuscript)
    return CompileResult.model_validate(result)


@router.get("/manuscripts/{manuscript_id}/compile/latest", response_model=CompileResult)
async def latest_compile_result(
    manuscript_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> CompileResult:
    manuscript = await _member_manuscript(session, manuscript_id, user)
    if not manuscript.latest_compile:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="NO_COMPILE_YET")
    return CompileResult.model_validate(manuscript.latest_compile)


@router.get("/manuscripts/{manuscript_id}/pdf")
async def get_pdf(
    manuscript_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> FileResponse:
    manuscript = await _member_manuscript(session, manuscript_id, user)
    path = latex_compile.latest_ok_pdf(manuscript)
    if path is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="PDF_NOT_AVAILABLE")
    return FileResponse(
        path,
        media_type="application/pdf",
        filename="main.pdf",
        content_disposition_type="inline",
    )


@router.get("/manuscripts/{manuscript_id}/export/arxiv")
async def export_arxiv(
    manuscript_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> Response:
    """arXiv 提交用清洁源码包（tar.gz）：源文件 + references.bib + figures + .bbl，
    剔除 aux/log/pdf 等。导出时重编一遍以生成与当前源一致的 .bbl。提示信息放响应头。"""
    manuscript = await _member_manuscript(session, manuscript_id, user, with_files=True)
    data, notes = await latex_compile.build_arxiv_tarball(session, manuscript)
    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in manuscript.title)[:60] or "paper"
    headers = {"Content-Disposition": f'attachment; filename="{safe}-arxiv.tar.gz"'}
    if notes:
        headers["X-Export-Notes"] = " | ".join(notes)
    return Response(content=data, media_type="application/gzip", headers=headers)


# ---- §5 AI 起草 ----


@router.post(
    "/manuscripts/{manuscript_id}/draft",
    response_model=VoyageRead,
    status_code=status.HTTP_201_CREATED,
)
async def draft_manuscript(
    manuscript_id: uuid.UUID,
    data: DraftRequest | None = None,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_writer),
    queue: TaskQueue = Depends(get_task_queue),
) -> VoyageRead:
    manuscript = await _member_manuscript(session, manuscript_id, user)
    data = data or DraftRequest()
    try:
        run = await manuscripts_service.create_writing_voyage(
            session,
            manuscript=manuscript,
            sections=data.sections,
            notes=data.notes,
            created_by=user.id,
        )
    except manuscripts_service.WritingInProgressError as e:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="WRITING_IN_PROGRESS") from e
    except manuscripts_service.InvalidSectionsError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail="INVALID_SECTIONS") from e
    await queue.enqueue("run_voyage", str(run.id))
    return VoyageRead.model_validate(run)


@router.post(
    "/manuscripts/{manuscript_id}/initialize-structure",
    response_model=ManuscriptFileContent,
)
async def initialize_structure(
    manuscript_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_writer),
) -> ManuscriptFileContent:
    """AI 起草前置：基于当前编译主文件新建 draft.tex（preamble 保留 + POLARIS_SECTION
    骨架正文），原主文件不动，并把编译主文件切到 draft.tex。返回新建的 draft.tex。
    """
    manuscript = await _member_manuscript(session, manuscript_id, user, with_files=True)
    main_path = manuscript.main_tex or "main.tex"
    if not any(f.path == main_path for f in manuscript.files):
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="MAIN_TEX_NOT_FOUND")
    try:
        draft, content = await manuscripts_service.initialize_structure(
            session, manuscript, user_id=user.id
        )
    except manuscripts_service.StructureError as e:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, detail="MAIN_TEX_NO_DOCUMENT"
        ) from e
    return ManuscriptFileContent(
        id=draft.id, path=draft.path, content=content, readonly=draft.readonly
    )


# ---- §6 内联 AI 写作辅助（SSE 流） ----

_ASSIST_HEARTBEAT_SECONDS = 15.0


def _sse_frame(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"


@router.post("/manuscripts/{manuscript_id}/assist")
async def assist_manuscript(
    manuscript_id: uuid.UUID,
    data: AssistRequest,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_writer),
) -> StreamingResponse:
    """选中润色/按指示改写/光标续写：stage=writing 流式输出。

    事件：delta（文本增量）* → warnings（越界引用/图表提示，可无）→ done；
    异常转 error 事件后关流。15s 心跳注释防代理断连。
    """
    manuscript = await _member_manuscript(session, manuscript_id, user)
    if data.mode in ("polish", "rewrite") and not data.text.strip():
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail="ASSIST_TEXT_REQUIRED")
    if data.mode == "rewrite" and not data.instruction.strip():
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, detail="ASSIST_INSTRUCTION_REQUIRED"
        )
    if data.mode == "continue" and not data.before.strip():
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail="ASSIST_BEFORE_REQUIRED")

    messages = writing_assist.build_assist_messages(
        manuscript,
        mode=data.mode,
        text=data.text,
        instruction=data.instruction,
        before=data.before,
        after=data.after,
    )
    fact_pack = manuscript.fact_pack
    project_id = manuscript.project_id
    llm = get_llm_router()

    async def event_stream() -> AsyncIterator[str]:
        queue: asyncio.Queue[tuple[str, str | None]] = asyncio.Queue()

        async def pump() -> None:
            try:
                # router.stream 结束时自动写 LLMUsage 记账（归属 user + project）
                async for chunk in llm.stream(
                    "writing", messages, user_id=user.id, project_id=project_id
                ):
                    await queue.put(("delta", chunk))
                await queue.put(("done", None))
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 — 转成 error 事件后关流
                logger.warning("manuscript assist stream failed", exc_info=True)
                await queue.put(("error", f"{type(e).__name__}: {e}"))

        task = asyncio.create_task(pump())
        collected: list[str] = []
        try:
            while True:
                try:
                    kind, payload = await asyncio.wait_for(
                        queue.get(), timeout=_ASSIST_HEARTBEAT_SECONDS
                    )
                except TimeoutError:
                    yield ": ping\n\n"
                    continue
                if kind == "delta":
                    collected.append(payload or "")
                    yield _sse_frame("delta", {"text": payload})
                elif kind == "done":
                    result = "".join(collected)
                    warnings = writing_assist.scan_result_warnings(fact_pack, result)
                    if warnings:
                        yield _sse_frame("warnings", {"items": warnings})
                    usage = {
                        "prompt_tokens": sum(estimate_tokens(m.content) for m in messages),
                        "completion_tokens": estimate_tokens(result),
                    }
                    yield _sse_frame("done", {"usage": usage})
                    return
                else:  # error
                    yield _sse_frame("error", {"detail": payload})
                    return
        finally:
            task.cancel()

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # nginx 关缓冲
        },
    )


# ---- M5-C 论文评审 ----


@router.post(
    "/manuscripts/{manuscript_id}/review",
    response_model=VoyageRead,
    status_code=status.HTTP_201_CREATED,
)
async def review_manuscript(
    manuscript_id: uuid.UUID,
    data: PaperReviewRequest | None = None,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_paper_review),
    queue: TaskQueue = Depends(get_task_queue),
) -> VoyageRead:
    manuscript = await _member_manuscript(session, manuscript_id, user)
    personas = (
        [p.model_dump() for p in data.personas] if data is not None and data.personas else None
    )
    try:
        run = await paper_review_service.create_review_voyage(
            session, manuscript=manuscript, personas=personas, created_by=user.id
        )
    except manuscripts_service.CompileRequiredError as e:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="COMPILE_REQUIRED") from e
    except paper_review_service.ReviewInProgressError as e:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="REVIEW_IN_PROGRESS") from e
    await queue.enqueue("run_voyage", str(run.id))
    return VoyageRead.model_validate(run)


@router.get("/manuscripts/{manuscript_id}/reviews", response_model=list[PaperReviewSummary])
async def list_manuscript_reviews(
    manuscript_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> list[PaperReviewSummary]:
    """评审历史（新→旧）；消息明细复用 GET /sessions/{sid}/messages。"""
    manuscript = await _member_manuscript(session, manuscript_id, user)
    rows = await paper_review_service.list_manuscript_reviews(session, manuscript.id)
    summaries = []
    for review_session, message_count in rows:
        payload = review_session.payload or {}
        summaries.append(
            PaperReviewSummary(
                session_id=review_session.id,
                created_at=review_session.created_at,
                status=review_session.status,
                passed=payload.get("passed"),
                meta=payload.get("meta"),
                message_count=message_count,
            )
        )
    return summaries


# ---- §7 投稿 ----


@router.post(
    "/manuscripts/{manuscript_id}/submit",
    response_model=GateRead,
    status_code=status.HTTP_201_CREATED,
)
async def submit_manuscript(
    manuscript_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
    bus: EventBus = Depends(get_event_bus),
) -> GateRead:
    manuscript = await _member_manuscript(session, manuscript_id, user)
    try:
        gate = await manuscripts_service.submit_manuscript(
            session, manuscript=manuscript, user_id=user.id
        )
    except manuscripts_service.CompileRequiredError as e:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="COMPILE_REQUIRED") from e
    except manuscripts_service.ReviewRequiredError as e:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="REVIEW_REQUIRED") from e
    gate_read = GateRead.model_validate(gate)
    await bus.publish_notify(
        manuscript.project_id,
        {"type": "gate.created", "gate": gate_read.model_dump(mode="json")},
    )
    await bus.publish_notify(
        manuscript.project_id,
        {
            "type": "manuscript.status",
            "manuscript_id": str(manuscript.id),
            "status": manuscript.status,
        },
    )
    return gate_read
