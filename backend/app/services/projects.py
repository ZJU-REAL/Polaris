"""项目业务逻辑（不 import fastapi）。"""

import json
import re
import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.llm.base import Message
from app.core.llm.router import get_llm_router
from app.models.project import Project, ProjectMember
from app.models.user import User
from app.schemas.project import ProjectCreate, ProjectDefinition, ProjectUpdate

_SLUG_RE = re.compile(r"[^a-z0-9]+")

# 稀疏 definition 缺 arxiv_categories 时的检索默认分类（actions_wiki 也用）
DEFAULT_ARXIV_CATEGORIES = ["cs.CL", "cs.AI", "cs.LG"]


def slugify(name: str) -> str:
    slug = _SLUG_RE.sub("-", name.lower()).strip("-")
    return slug or uuid.uuid4().hex[:8]


async def _unique_slug(session: AsyncSession, base: str) -> str:
    slug = base
    while (await session.execute(select(Project.id).where(Project.slug == slug))).first():
        slug = f"{base}-{uuid.uuid4().hex[:6]}"
    return slug


async def list_projects(session: AsyncSession, user_id: uuid.UUID) -> Sequence[Project]:
    """列出用户参与（作为成员）的全部项目。"""
    stmt = (
        select(Project)
        .join(ProjectMember, ProjectMember.project_id == Project.id)
        .where(ProjectMember.user_id == user_id)
        .order_by(Project.created_at.desc())
    )
    return (await session.execute(stmt)).scalars().all()


async def create_project(
    session: AsyncSession, owner_id: uuid.UUID, data: ProjectCreate
) -> Project:
    """建项目并把 owner 记为成员（role=owner）。"""
    slug = await _unique_slug(session, data.slug or slugify(data.name))
    project = Project(
        name=data.name,
        slug=slug,
        definition=data.definition,
        owner_id=owner_id,
    )
    session.add(project)
    await session.flush()
    session.add(ProjectMember(project_id=project.id, user_id=owner_id, role="owner"))
    await session.commit()
    await session.refresh(project)
    return project


async def get_project(
    session: AsyncSession, project_id: uuid.UUID, user_id: uuid.UUID
) -> Project | None:
    """取项目；非成员视为不存在（返回 None）。"""
    stmt = (
        select(Project)
        .join(ProjectMember, ProjectMember.project_id == Project.id)
        .where(Project.id == project_id, ProjectMember.user_id == user_id)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def list_members(session: AsyncSession, project_id: uuid.UUID) -> list[dict[str, object]]:
    """项目成员（附 email / display_name，供 detail 返回）。"""
    stmt = (
        select(ProjectMember, User.email, User.display_name)
        .join(User, User.id == ProjectMember.user_id)
        .where(ProjectMember.project_id == project_id)
        .order_by(ProjectMember.created_at)
    )
    return [
        {
            "project_id": member.project_id,
            "user_id": member.user_id,
            "role": member.role,
            "email": email,
            "display_name": display_name,
        }
        for member, email, display_name in (await session.execute(stmt)).all()
    ]


def can_manage_project(project: Project, user: User) -> bool:
    """PATCH / 加成员权限：项目 owner 或平台 admin。"""
    return project.owner_id == user.id or user.role == "admin"


async def update_project(session: AsyncSession, project: Project, data: ProjectUpdate) -> Project:
    if data.name is not None:
        project.name = data.name
    if data.definition is not None:
        project.definition = data.definition
    if data.status is not None:
        project.status = data.status
    await session.commit()
    await session.refresh(project)
    return project


async def delete_project(session: AsyncSession, project: Project) -> None:
    """删除项目；论文/概念/任务等子表靠 FK ondelete=CASCADE 一并清除。"""
    await session.delete(project)
    await session.commit()


async def add_member(
    session: AsyncSession, project_id: uuid.UUID, *, email: str, role: str
) -> bool:
    """按 email 把用户加入项目（已是成员则更新角色）。用户不存在返回 False。"""
    user = (await session.execute(select(User).where(User.email == email))).scalar_one_or_none()
    if user is None:
        return False
    member = await session.get(ProjectMember, (project_id, user.id))
    if member is None:
        session.add(ProjectMember(project_id=project_id, user_id=user.id, role=role))
    else:
        member.role = role
    await session.commit()
    return True


# ---- LLM 起草研究方向定义（stage=interview） ----

_DRAFT_MAX_ATTEMPTS = 3  # 首次 + 解析失败重试 2 次

DRAFT_DEFINITION_SYSTEM_PROMPT = """\
你是研究方向访谈助理，根据用户的一句话方向定义（statement）起草完整的研究方向定义。
只输出一个 JSON 对象，不要输出任何其他文字或 Markdown 代码块，格式：
{"statement": "原样保留用户的 statement",
 "goals": ["2-4 个研究目标"],
 "in_scope": ["范围内主题"],
 "out_of_scope": ["范围外主题"],
 "questions": ["3-5 个具体研究问题"],
 "rubric": [{"name": "维度名", "description": "打分标准", "weight": 1.0}],
 "keywords": {"arxiv_categories": ["cs.CL"], "include": ["检索关键词"],
              "synonyms": {"术语": ["同义词"]}},
 "anchor_papers": [],
 "cadence": "daily"}
要求：rubric 给 1-3 个维度；arxiv_categories 从常见 cs.* 分类中选择合理项；
keywords.include 必须保留用户给出的关键词并可适当扩充；
anchor_papers 必须是空数组，不要编造论文。
"""


def _dedup_keep_order(items: Sequence[str]) -> list[str]:
    return list(dict.fromkeys(s.strip() for s in items if s and s.strip()))


def _extract_json_object(content: str) -> Any:
    start = content.find("{")
    end = content.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object found")
    return json.loads(content[start : end + 1])


def fallback_definition(statement: str, keywords_include: Sequence[str]) -> ProjectDefinition:
    """LLM 不可用/解析失败时的规则回退草稿：用 statement 与用户关键词拼合理默认值。"""
    statement = statement.strip()
    return ProjectDefinition(
        statement=statement,
        goals=[f"梳理「{statement}」的研究现状与代表性方法", "识别该方向的关键开放问题"],
        in_scope=[statement],
        out_of_scope=[],
        questions=[
            f"「{statement}」当前的主流方法有哪些？",
            "现有方法的主要局限与失效场景是什么？",
            "有哪些可行的改进方向与评测方式？",
        ],
        rubric=[{"name": "relevance", "description": "与研究方向的相关程度", "weight": 1.0}],
        anchor_papers=[],
        keywords={
            "arxiv_categories": list(DEFAULT_ARXIV_CATEGORIES),
            "include": _dedup_keep_order(keywords_include),
            "synonyms": {},
        },
        cadence="daily",
    )


def _finalize_draft(
    draft: ProjectDefinition, statement: str, keywords_include: Sequence[str]
) -> ProjectDefinition:
    """LLM 草稿后处理：statement 原样保留、锚点清空、用户关键词兜底、分类兜底。"""
    draft.statement = statement.strip()
    draft.anchor_papers = []  # 不允许 LLM 编造论文
    draft.keywords.include = _dedup_keep_order(
        list(keywords_include) + list(draft.keywords.include)
    )
    if not draft.keywords.arxiv_categories:
        draft.keywords.arxiv_categories = list(DEFAULT_ARXIV_CATEGORIES)
    if not draft.cadence:
        draft.cadence = "daily"
    return draft


async def draft_definition(
    *,
    statement: str,
    name: str | None = None,
    keywords_include: Sequence[str] | None = None,
    user_id: uuid.UUID | None = None,
) -> tuple[ProjectDefinition, str]:
    """stage=interview 起草完整 definition；LLM 失败时回退规则草稿。

    返回 (definition, source)，source ∈ {"llm", "fallback"}。
    """
    keywords_include = list(keywords_include or [])
    user_prompt = (
        f"方向定义：{statement.strip()}\n"
        f"名称：{name or '（未提供）'}\n"
        f"用户关键词：{json.dumps(keywords_include, ensure_ascii=False)}"
    )
    messages = [
        Message(role="system", content=DRAFT_DEFINITION_SYSTEM_PROMPT),
        Message(role="user", content=user_prompt),
    ]
    llm = get_llm_router()
    for _attempt in range(_DRAFT_MAX_ATTEMPTS):
        try:
            result = await llm.complete("interview", messages, user_id=user_id)
            draft = ProjectDefinition.model_validate(_extract_json_object(result.content))
        except Exception:  # noqa: BLE001 — 调用失败/非 JSON/结构不合法均重试
            continue
        return _finalize_draft(draft, statement, keywords_include), "llm"
    return fallback_definition(statement, keywords_include), "fallback"
