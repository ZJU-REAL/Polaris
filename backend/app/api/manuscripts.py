"""论文撰写路由（docs/api-m5-b.md §1/§2/§3/§4/§5/§7）。

权限：一律项目成员（非成员 404 不泄露存在性）；DELETE 稿件仅 owner/admin。
编译为同步端点（tectonic 硬超时 120s）；实时协同走 /ws/manuscripts/{fid}（api/ws.py）。
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import current_active_user, require_paper_review, require_writer
from app.core.db import get_session
from app.core.events import EventBus, get_event_bus
from app.core.queue import TaskQueue, get_task_queue
from app.models.manuscript import Manuscript, ManuscriptFile
from app.models.user import User
from app.schemas.gate import GateRead
from app.schemas.manuscript import (
    CompileResult,
    DraftRequest,
    ManuscriptCreate,
    ManuscriptDetail,
    ManuscriptFileBrief,
    ManuscriptFileContent,
    ManuscriptFileCreate,
    ManuscriptFileRename,
    ManuscriptRead,
    ManuscriptUpdate,
    TemplateInfo,
)
from app.schemas.review import PaperReviewRequest, PaperReviewSummary
from app.schemas.voyage import VoyageRead
from app.services import latex_compile
from app.services import manuscripts as manuscripts_service
from app.services import paper_review as paper_review_service
from app.services import projects as projects_service
from app.services.crdt_rooms import get_crdt_rooms

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
    return ManuscriptFileBrief(
        id=file.id,
        path=file.path,
        size=len(file.content.encode("utf-8")),
        readonly=file.readonly,
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
async def list_templates(user: User = Depends(current_active_user)) -> list[TemplateInfo]:
    return [TemplateInfo.model_validate(m) for m in manuscripts_service.list_templates()]


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
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> list[ManuscriptRead]:
    project = await projects_service.get_project(session, project_id=project_id, user_id=user.id)
    if project is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="PROJECT_NOT_FOUND")
    rows = await manuscripts_service.list_manuscripts(session, project_id=project_id)
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
    manuscript = await _member_manuscript(session, manuscript_id, user)
    if data.title is not None:
        manuscript.title = data.title
        await session.commit()
        await session.refresh(manuscript)
    return ManuscriptRead.model_validate(manuscript)


@router.delete("/manuscripts/{manuscript_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_manuscript(
    manuscript_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> None:
    manuscript = await _member_manuscript(session, manuscript_id, user)
    project = await projects_service.get_project(
        session, project_id=manuscript.project_id, user_id=user.id
    )
    if project is None or not projects_service.can_manage_project(project, user):
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="OWNER_OR_ADMIN_REQUIRED")
    await session.delete(manuscript)
    await session.commit()


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
