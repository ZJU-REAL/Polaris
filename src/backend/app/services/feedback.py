"""用户反馈服务：落库 / 截图 / LLM 生成 issue 草稿 / 建 GitHub issue。"""

import io
import json
import re
import time
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import github
from app.core.config import get_settings
from app.core.llm.base import Message
from app.core.llm.router import get_llm_router
from app.models.feedback import Feedback, FeedbackImage
from app.models.user import User

_MAX_IMAGE_BYTES = 6 * 1024 * 1024
_MAX_IMAGE_DIM = 2000
_MAX_IMAGES = 8

# GitHub issue 模板骨架（镜像 .github/ISSUE_TEMPLATE/*.yml；容器不挂 .github，故内嵌）。
# 每个 feedback type 映射一个模板：标题前缀 + 默认 labels + 章节标题列表。
_AREA_OPTIONS = [
    "frontend",
    "backend-api",
    "voyage (task loop)",
    "literature / wiki",
    "writer",
    "review",
    "skills",
    "infra / deploy",
    "docs",
    "other / not sure",
]
_TEMPLATES: dict[str, dict[str, Any]] = {
    "bug": {
        "title_prefix": "bug: ",
        "labels": ["bug"],
        "sections": ["Summary", "Area", "Steps to reproduce", "Expected vs actual", "Environment"],
    },
    "feature": {
        "title_prefix": "feat: ",
        "labels": ["enhancement"],
        "sections": [
            "Problem / motivation",
            "Area",
            "Proposed solution",
            "Alternatives considered",
        ],
    },
    "task": {
        "title_prefix": "task: ",
        "labels": ["task"],
        "sections": ["Description", "Area", "Acceptance criteria"],
    },
}
# feedback.type → 使用哪个模板
_TYPE_TO_TEMPLATE = {
    "bug": "bug",
    "ui": "bug",
    "perf": "bug",
    "feature": "feature",
    "task": "task",
    "question": "task",
    "other": "task",
}

# 路由前缀 → 前端 feature 目录名（给 CC 指路；最长前缀优先）
_ROUTE_MODULE = [
    ("/papers/", "reading"),
    ("/wiki", "wiki"),
    ("/forge", "forge"),
    ("/ideas/", "forge"),
    ("/review", "review"),
    ("/paper-review", "paper-review"),
    ("/experiment", "experiment"),
    ("/writer", "writer"),
    ("/voyages", "voyages"),
    ("/projects", "projects"),
    ("/mcp-tools", "mcp"),
    ("/skills", "skills"),
    ("/settings", "settings"),
]


def module_from_route(route: str | None) -> str | None:
    if not route:
        return None
    for prefix, mod in _ROUTE_MODULE:
        if route.startswith(prefix):
            return mod
    if route == "/" or route.startswith("/dashboard"):
        return "dashboard"
    return None


# ---- CRUD ----


async def create_feedback(
    session: AsyncSession, *, user_id: uuid.UUID, data: dict[str, Any]
) -> Feedback:
    fb = Feedback(
        user_id=user_id,
        type=data.get("type", "bug"),
        severity=data.get("severity", "normal"),
        title=data["title"].strip()[:255],
        body=(data.get("body") or "").strip(),
        route=data.get("route"),
        module=module_from_route(data.get("route")),
        context=data.get("context"),
    )
    session.add(fb)
    await session.commit()
    await session.refresh(fb)
    return fb


def _feedback_dir(feedback_id: uuid.UUID) -> Path:
    d = Path(get_settings().data_dir) / "feedback" / str(feedback_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


async def add_image(session: AsyncSession, feedback: Feedback, raw: bytes) -> FeedbackImage:
    """校验并落盘一张截图（统一转 PNG，超大边长缩放），返回记录。"""
    from PIL import Image

    if len(raw) > _MAX_IMAGE_BYTES:
        raise ValueError("IMAGE_TOO_LARGE")
    count = (
        await session.execute(
            select(FeedbackImage.id).where(FeedbackImage.feedback_id == feedback.id)
        )
    ).all()
    if len(count) >= _MAX_IMAGES:
        raise ValueError("TOO_MANY_IMAGES")
    try:
        img = Image.open(io.BytesIO(raw))
        img.load()
    except Exception as e:
        raise ValueError("NOT_IMAGE") from e
    if max(img.size) > _MAX_IMAGE_DIM:
        img.thumbnail((_MAX_IMAGE_DIM, _MAX_IMAGE_DIM), Image.LANCZOS)
    seq = len(count)
    path = _feedback_dir(feedback.id) / f"{seq}.png"
    img.convert("RGBA").save(path, format="PNG")
    rec = FeedbackImage(feedback_id=feedback.id, path=str(path), seq=seq)
    session.add(rec)
    await session.commit()
    await session.refresh(rec)
    return rec


async def list_feedback(
    session: AsyncSession, *, user_id: uuid.UUID | None = None
) -> Sequence[Feedback]:
    stmt = select(Feedback).order_by(Feedback.created_at.desc())
    if user_id is not None:
        stmt = stmt.where(Feedback.user_id == user_id)
    return (await session.execute(stmt)).scalars().all()


async def images_for(session: AsyncSession, feedback_id: uuid.UUID) -> Sequence[FeedbackImage]:
    stmt = (
        select(FeedbackImage)
        .where(FeedbackImage.feedback_id == feedback_id)
        .order_by(FeedbackImage.seq)
    )
    return (await session.execute(stmt)).scalars().all()


async def author_of(session: AsyncSession, feedback: Feedback) -> User | None:
    if feedback.user_id is None:
        return None
    return await session.get(User, feedback.user_id)


async def admin_update(
    session: AsyncSession, feedback: Feedback, payload: dict[str, Any]
) -> Feedback:
    for field in ("status", "severity", "type", "admin_note"):
        if field in payload and payload[field] is not None:
            setattr(feedback, field, payload[field])
    await session.commit()
    await session.refresh(feedback)
    return feedback


# ---- GitHub issue 状态回同步 ----

# 已建 issue 但仍未终结的反馈状态：这些才需要去 GitHub 查最新 state
_SYNCABLE_STATUSES = ("new", "triaged", "in_progress")
# 每条反馈两次查询之间的最短间隔（秒）：列表页高频刷新时不反复打 GitHub
_SYNC_TTL_SECONDS = 300.0
_last_synced: dict[uuid.UUID, float] = {}


async def sync_issue_statuses(session: AsyncSession, rows: Sequence[Feedback]) -> None:
    """把已关联 GitHub issue 的反馈状态与 issue state 对齐（closed → resolved）。

    best-effort：GitHub 未配置 / 网络失败都静默跳过，不影响列表返回。
    带 TTL 节流，同一条反馈 5 分钟内只查一次。
    """
    now = time.monotonic()
    pending = [
        fb
        for fb in rows
        if fb.github_issue_number is not None
        and fb.status in _SYNCABLE_STATUSES
        and now - _last_synced.get(fb.id, -_SYNC_TTL_SECONDS) >= _SYNC_TTL_SECONDS
    ]
    if not pending:
        return
    numbers = [fb.github_issue_number for fb in pending if fb.github_issue_number is not None]
    states = await github.fetch_issue_states(numbers)
    changed = False
    for fb in pending:
        state = states.get(fb.github_issue_number)
        if state is None:
            continue  # 单条查询失败：不刷新节流时间，下次再试
        _last_synced[fb.id] = now
        if state == "closed":
            fb.status = "resolved"
            changed = True
    if changed:
        await session.commit()


# ---- LLM issue 草稿 ----

_SYSTEM_PROMPT = (
    "You turn a raw user feedback report from the Polaris research platform into a clean, "
    "actionable GitHub issue that an engineer (and an AI coding agent) can act on directly. "
    "Follow the given issue template exactly. Respond in English. "
    "Output STRICT JSON only, no prose, shaped as: "
    '{"title": string, "body": string, "labels": [string]}. '
    "The title MUST start with the template's title prefix and be a concise summary. "
    "The body MUST be GitHub-flavored markdown whose sections are the template's section labels "
    "rendered as '### <label>'. Fill each section from the feedback; if a section's info is "
    "missing, state that briefly rather than inventing details. For the 'Area' section, choose "
    "exactly one value from the provided area options based on the route/module hint. "
    "Do not include screenshots — they are attached separately."
)


def _draft_user_prompt(feedback: Feedback, template: dict[str, Any]) -> str:
    ctx = feedback.context or {}
    return (
        f"## Raw feedback\n"
        f"- type: {feedback.type}\n"
        f"- severity: {feedback.severity}\n"
        f"- title: {feedback.title}\n"
        f"- description / steps (verbatim):\n{feedback.body or '(none)'}\n\n"
        f"## Context (auto-captured)\n"
        f"- route: {feedback.route or '(unknown)'}\n"
        f"- module hint (frontend feature dir): {feedback.module or '(unknown)'}\n"
        f"- app version: {ctx.get('version', '?')}\n"
        f"- environment: {ctx.get('env', '?')}\n"
        f"- viewport: {ctx.get('viewport', '?')}, UA: {ctx.get('ua', '?')}\n"
        f"- research direction: {ctx.get('project', '?')}\n\n"
        f"## Target template\n"
        f"- title prefix: {template['title_prefix']!r}\n"
        f"- default labels: {template['labels']}\n"
        f"- sections (use as '### <label>'): {template['sections']}\n"
        f"- Area options (pick one): {_AREA_OPTIONS}\n\n"
        f"Produce the issue JSON now."
    )


def _extract_json(text: str) -> dict[str, Any]:
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        raise ValueError("no JSON object in LLM output")
    return json.loads(m.group(0))


def _fallback_draft(feedback: Feedback, template: dict[str, Any]) -> dict[str, Any]:
    """LLM 不可用时的规则草稿：把原始反馈塞进模板骨架。"""
    ctx = feedback.context or {}
    lines = [f"### {template['sections'][0]}", feedback.title, ""]
    if feedback.body:
        lines += [feedback.body, ""]
    lines += [
        "### Area",
        feedback.module or "other / not sure",
        "",
        "### Context",
        f"- route: {feedback.route or '?'}",
        f"- version: {ctx.get('version', '?')} · env: {ctx.get('env', '?')}",
    ]
    title = feedback.title
    if not title.lower().startswith(template["title_prefix"].strip().rstrip(":")):
        title = template["title_prefix"] + title
    return {"title": title[:255], "body": "\n".join(lines), "labels": list(template["labels"])}


async def generate_issue_draft(session: AsyncSession, feedback: Feedback) -> dict[str, Any]:
    """用 LLM 按仓库模板把反馈改写成 issue 草稿；失败回退规则草稿。存到 feedback.issue_draft。"""
    template = _TEMPLATES[_TYPE_TO_TEMPLATE.get(feedback.type, "task")]
    messages = [
        Message(role="system", content=_SYSTEM_PROMPT),
        Message(role="user", content=_draft_user_prompt(feedback, template)),
    ]
    draft: dict[str, Any] | None = None
    try:
        result = await get_llm_router().complete(
            "feedback_issue", messages, user_id=feedback.user_id
        )
        parsed = _extract_json(result.content)
        title = str(parsed.get("title", "")).strip()
        body = str(parsed.get("body", "")).strip()
        if title and body:
            prefix = template["title_prefix"]
            if not title.lower().startswith(prefix.strip().rstrip(":").lower()):
                title = prefix + title
            labels = parsed.get("labels") or template["labels"]
            draft = {"title": title[:255], "body": body, "labels": list(labels)}
    except Exception:  # noqa: BLE001 — 调用失败/非 JSON 均回退
        draft = None
    if draft is None:
        draft = _fallback_draft(feedback, template)
    feedback.issue_draft = draft
    await session.commit()
    await session.refresh(feedback)
    return draft


# ---- 建 GitHub issue ----


async def create_issue_from_draft(
    session: AsyncSession, feedback: Feedback, draft: dict[str, Any]
) -> tuple[int, str]:
    """用（可能被 admin 编辑过的）草稿建 issue，回填 number/url，状态置 in_progress。"""
    number, url = await github.create_issue(
        title=draft["title"], body=draft["body"], labels=draft.get("labels") or []
    )
    feedback.github_issue_number = number
    feedback.github_issue_url = url
    feedback.issue_draft = draft
    if feedback.status in ("new", "triaged"):
        feedback.status = "in_progress"
    await session.commit()
    await session.refresh(feedback)
    return number, url
