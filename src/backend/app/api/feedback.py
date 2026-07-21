"""用户反馈：提交（含截图）/ 查看自己的；管理员 triage / LLM 草稿 / 建 GitHub issue。"""

import contextlib
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, UploadFile, status
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import current_active_user, require_admin
from app.core import github
from app.core.db import get_session
from app.models.feedback import Feedback
from app.models.user import User
from app.schemas.feedback import (
    AdminFeedbackUpdate,
    FeedbackAuthor,
    FeedbackCreate,
    FeedbackImageRead,
    FeedbackRead,
    IssueCreateResult,
    IssueDraft,
)
from app.services import feedback as svc

router = APIRouter(tags=["feedback"])


async def _to_read(session: AsyncSession, fb: Feedback) -> FeedbackRead:
    images = await svc.images_for(session, fb.id)
    author = await svc.author_of(session, fb)
    return FeedbackRead(
        id=fb.id,
        type=fb.type,
        severity=fb.severity,
        title=fb.title,
        body=fb.body,
        route=fb.route,
        module=fb.module,
        context=fb.context,
        status=fb.status,
        admin_note=fb.admin_note,
        issue_draft=fb.issue_draft,
        github_issue_number=fb.github_issue_number,
        github_issue_url=fb.github_issue_url,
        created_at=fb.created_at,
        images=[FeedbackImageRead(id=i.id, seq=i.seq) for i in images],
        author=(
            FeedbackAuthor(id=author.id, display_name=author.display_name, username=author.username)
            if author
            else None
        ),
    )


async def _load(session: AsyncSession, feedback_id: uuid.UUID) -> Feedback:
    fb = await session.get(Feedback, feedback_id)
    if fb is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="FEEDBACK_NOT_FOUND")
    return fb


# ---- 用户端 ----


@router.post("/feedback", response_model=FeedbackRead, status_code=status.HTTP_201_CREATED)
async def submit_feedback(
    data: FeedbackCreate,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> FeedbackRead:
    fb = await svc.create_feedback(session, user_id=user.id, data=data.model_dump())
    return await _to_read(session, fb)


@router.post("/feedback/{feedback_id}/images", response_model=FeedbackImageRead)
async def upload_feedback_image(
    feedback_id: uuid.UUID,
    file: UploadFile,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> FeedbackImageRead:
    fb = await _load(session, feedback_id)
    if fb.user_id != user.id and user.role != "admin":
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="NOT_OWNER")
    raw = await file.read()
    try:
        rec = await svc.add_image(session, fb, raw)
    except ValueError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e)) from e
    return FeedbackImageRead(id=rec.id, seq=rec.seq)


@router.get("/feedback/mine", response_model=list[FeedbackRead])
async def my_feedback(
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> list[FeedbackRead]:
    rows = await svc.list_feedback(session, user_id=user.id)
    with contextlib.suppress(Exception):  # 状态同步失败不影响列表
        await svc.sync_issue_statuses(session, rows)
    return [await _to_read(session, fb) for fb in rows]


@router.get("/feedback/{feedback_id}/images/{seq}")
async def get_feedback_image(
    feedback_id: uuid.UUID,
    seq: int,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> FileResponse:
    fb = await _load(session, feedback_id)
    if fb.user_id != user.id and user.role != "admin":
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="NOT_OWNER")
    images = await svc.images_for(session, feedback_id)
    match = next((i for i in images if i.seq == seq), None)
    if match is None or not Path(match.path).is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="IMAGE_NOT_FOUND")
    return FileResponse(match.path, media_type="image/png")


# ---- 管理端 ----


@router.get("/admin/feedback", response_model=list[FeedbackRead])
async def admin_list_feedback(
    session: AsyncSession = Depends(get_session),
    _: User = Depends(require_admin),
) -> list[FeedbackRead]:
    rows = await svc.list_feedback(session)
    with contextlib.suppress(Exception):  # 状态同步失败不影响列表
        await svc.sync_issue_statuses(session, rows)
    return [await _to_read(session, fb) for fb in rows]


@router.get("/admin/feedback/github-status")
async def admin_github_status(_: User = Depends(require_admin)) -> dict[str, bool]:
    return {"enabled": github.github_enabled()}


@router.patch("/admin/feedback/{feedback_id}", response_model=FeedbackRead)
async def admin_update_feedback(
    feedback_id: uuid.UUID,
    data: AdminFeedbackUpdate,
    session: AsyncSession = Depends(get_session),
    _: User = Depends(require_admin),
) -> FeedbackRead:
    fb = await _load(session, feedback_id)
    fb = await svc.admin_update(session, fb, data.model_dump(exclude_unset=True))
    return await _to_read(session, fb)


@router.post("/admin/feedback/{feedback_id}/draft", response_model=IssueDraft)
async def admin_generate_draft(
    feedback_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    _: User = Depends(require_admin),
) -> IssueDraft:
    fb = await _load(session, feedback_id)
    draft = await svc.generate_issue_draft(session, fb)
    return IssueDraft(**draft)


@router.post("/admin/feedback/{feedback_id}/issue", response_model=IssueCreateResult)
async def admin_create_issue(
    feedback_id: uuid.UUID,
    draft: IssueDraft,
    session: AsyncSession = Depends(get_session),
    _: User = Depends(require_admin),
) -> IssueCreateResult:
    fb = await _load(session, feedback_id)
    if fb.github_issue_number is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="ISSUE_ALREADY_CREATED")
    try:
        number, url = await svc.create_issue_from_draft(session, fb, draft.model_dump())
    except github.GitHubNotConfigured as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="GITHUB_NOT_CONFIGURED") from e
    except github.GitHubError as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail=f"GITHUB_ERROR: {e}") from e
    return IssueCreateResult(number=number, url=url)
