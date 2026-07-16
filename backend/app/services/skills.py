"""技能系统业务逻辑（docs/skill-system.md；不 import fastapi）。

- 技能 CRUD / 版本追加 / fork / 归档（builtin 只读）
- 启用到项目（project_skills）
- Voyage 快照：把项目生效技能解析成 checkpoint["skills"] 结构（§3.2）
- 内置技能种子幂等插入
"""

import json
import uuid
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

if TYPE_CHECKING:
    from app.models.voyage import VoyageRun

from app.models.skill import ProjectSkill, Skill, SkillVersion
from app.schemas.skill import (
    ProjectSkillCreate,
    ProjectSkillUpdate,
    SkillCreate,
    SkillManifest,
    SkillVersionCreate,
)
from app.services.builtin_skills import BUILTIN_SKILLS


class SkillSlugConflictError(Exception):
    """同 scope 下 slug 已存在。"""


class SkillReadOnlyError(Exception):
    """内置技能 / 非本人技能不可修改。"""


class SkillWorkflowInvalidError(Exception):
    """workflow 技能 steps 不符合 Navigator 步骤 schema。"""


# ---- 校验 ----


def _validate_workflow_steps(manifest: SkillManifest) -> None:
    """workflow 技能保存时即校验 steps（动作必须在注册表白名单内）。"""
    if not manifest.steps:
        raise SkillWorkflowInvalidError("workflow skill requires non-empty steps")
    # 惰性 import：确保动作注册表已由 app.agents.voyage 包加载
    import app.agents.voyage  # noqa: F401
    from app.agents.voyage.navigator import validate_steps

    try:
        validate_steps({"steps": manifest.steps})
    except ValueError as e:
        raise SkillWorkflowInvalidError(str(e)) from e


def _check_manifest(kind: str, manifest: SkillManifest) -> None:
    if kind == "workflow":
        _validate_workflow_steps(manifest)
    elif kind == "persona" and not manifest.personas:
        raise SkillWorkflowInvalidError("persona skill requires at least one persona")


async def _slug_taken(
    session: AsyncSession, slug: str, *, owner_id: uuid.UUID | None, scope: str
) -> bool:
    """builtin slug 全局唯一；user 技能 slug 与 builtin 及本人技能都不冲突。"""
    stmt = select(Skill.id).where(Skill.slug == slug, Skill.is_archived.is_(False))
    if scope != "builtin":
        stmt = stmt.where((Skill.scope == "builtin") | (Skill.owner_id == owner_id))
    return (await session.execute(stmt)).first() is not None


# ---- CRUD ----


async def create_skill(
    session: AsyncSession,
    *,
    owner_id: uuid.UUID,
    data: SkillCreate,
    scope: str = "user",
) -> Skill:
    if await _slug_taken(session, data.slug, owner_id=owner_id, scope=scope):
        raise SkillSlugConflictError(data.slug)
    _check_manifest(data.kind, data.manifest)
    skill = Skill(
        slug=data.slug,
        kind=data.kind,
        name=data.name,
        name_en=data.name_en,
        description=data.description,
        scope=scope,
        owner_id=owner_id,
    )
    session.add(skill)
    await session.flush()
    session.add(
        SkillVersion(
            skill_id=skill.id,
            version=1,
            manifest=data.manifest.model_dump(mode="json"),
            body=data.body,
            created_by=owner_id,
        )
    )
    await session.commit()
    await session.refresh(skill)
    return skill


async def list_skills(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    scope: str | None = None,
    kind: str | None = None,
    q: str | None = None,
) -> Sequence[Skill]:
    """内置技能全员可见；user 技能只见自己的。"""
    stmt = select(Skill).where(Skill.is_archived.is_(False))
    if scope == "builtin":
        stmt = stmt.where(Skill.scope == "builtin")
    elif scope == "mine":
        stmt = stmt.where(Skill.owner_id == user_id)
    else:
        stmt = stmt.where((Skill.scope == "builtin") | (Skill.owner_id == user_id))
    if kind:
        stmt = stmt.where(Skill.kind == kind)
    if q:
        pattern = f"%{q}%"
        stmt = stmt.where(Skill.name.ilike(pattern) | Skill.slug.ilike(pattern))
    stmt = stmt.order_by(Skill.scope, Skill.created_at.desc())
    return (await session.execute(stmt)).scalars().all()


async def get_skill(
    session: AsyncSession, skill_id: uuid.UUID, *, user_id: uuid.UUID
) -> Skill | None:
    """可见性同 list：builtin 或本人所有。"""
    skill = await session.get(Skill, skill_id)
    if skill is None or skill.is_archived:
        return None
    if skill.scope != "builtin" and skill.owner_id != user_id:
        return None
    return skill


async def latest_version(session: AsyncSession, skill_id: uuid.UUID) -> SkillVersion | None:
    stmt = (
        select(SkillVersion)
        .where(SkillVersion.skill_id == skill_id)
        .order_by(SkillVersion.version.desc())
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def list_versions(session: AsyncSession, skill_id: uuid.UUID) -> Sequence[SkillVersion]:
    stmt = (
        select(SkillVersion)
        .where(SkillVersion.skill_id == skill_id)
        .order_by(SkillVersion.version.desc())
    )
    return (await session.execute(stmt)).scalars().all()


async def add_version(
    session: AsyncSession,
    skill: Skill,
    *,
    user_id: uuid.UUID,
    data: SkillVersionCreate,
) -> SkillVersion:
    if skill.scope == "builtin" or skill.owner_id != user_id:
        raise SkillReadOnlyError(skill.slug)
    _check_manifest(skill.kind, data.manifest)
    current = await latest_version(session, skill.id)
    version = SkillVersion(
        skill_id=skill.id,
        version=(current.version if current else 0) + 1,
        manifest=data.manifest.model_dump(mode="json"),
        body=data.body,
        changelog=data.changelog,
        created_by=user_id,
    )
    session.add(version)
    await session.commit()
    await session.refresh(version)
    return version


async def fork_skill(session: AsyncSession, skill: Skill, *, user_id: uuid.UUID) -> Skill:
    """复制为我的技能（builtin / 市场技能的编辑路径）：拷贝当前版本为 user scope v1。"""
    src = await latest_version(session, skill.id)
    if src is None:
        raise SkillReadOnlyError(f"{skill.slug} has no version")
    slug = skill.slug
    if await _slug_taken(session, slug, owner_id=user_id, scope="user"):
        # builtin slug 必然冲突：追加短后缀直到可用
        for i in range(2, 100):
            candidate = f"{slug}-{i}"[:64]
            if not await _slug_taken(session, candidate, owner_id=user_id, scope="user"):
                slug = candidate
                break
        else:  # pragma: no cover — 防御分支
            raise SkillSlugConflictError(skill.slug)
    fork = Skill(
        slug=slug,
        kind=skill.kind,
        name=skill.name,
        name_en=skill.name_en,
        description=skill.description,
        scope="user",
        owner_id=user_id,
    )
    session.add(fork)
    await session.flush()
    session.add(
        SkillVersion(
            skill_id=fork.id,
            version=1,
            manifest=src.manifest,
            body=src.body,
            changelog=f"复制自 {skill.name}（{skill.slug} v{src.version}）",
            created_by=user_id,
        )
    )
    await session.commit()
    await session.refresh(fork)
    return fork


async def archive_skill(session: AsyncSession, skill: Skill, *, user_id: uuid.UUID) -> None:
    if skill.scope == "builtin" or skill.owner_id != user_id:
        raise SkillReadOnlyError(skill.slug)
    skill.is_archived = True
    await session.commit()


# ---- 启用到项目 ----


async def list_project_skills(
    session: AsyncSession, project_id: uuid.UUID
) -> Sequence[ProjectSkill]:
    stmt = (
        select(ProjectSkill)
        .where(ProjectSkill.project_id == project_id)
        .options(selectinload(ProjectSkill.skill))
        .order_by(ProjectSkill.target, ProjectSkill.sort_order, ProjectSkill.created_at)
    )
    return (await session.execute(stmt)).scalars().all()


async def enable_skill(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    user_id: uuid.UUID,
    data: ProjectSkillCreate,
    skill: Skill,
) -> ProjectSkill:
    """同一 (project, skill, target) 唯一（DB 约束），重复启用由调用方转 409。"""
    # 注入点必须是技能声明过的 target（防误挂）；manifest 未声明任何 target 时放行
    declared = _skill_targets(await _manifest_of(session, skill))
    if declared and data.target not in declared:
        raise SkillWorkflowInvalidError(f"skill {skill.slug} does not declare target {data.target}")
    row = ProjectSkill(
        project_id=project_id,
        skill_id=skill.id,
        version_id=data.version_id,
        target=data.target,
        config=data.config,
        sort_order=data.sort_order,
        created_by=user_id,
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


async def _manifest_of(session: AsyncSession, skill: Skill) -> dict[str, Any] | None:
    version = await latest_version(session, skill.id)
    return version.manifest if version else None


def _skill_targets(manifest: dict[str, Any] | None) -> list[str]:
    if not isinstance(manifest, dict):
        return []
    targets = manifest.get("targets")
    return [t for t in targets if isinstance(t, str)] if isinstance(targets, list) else []


async def get_project_skill(session: AsyncSession, enable_id: uuid.UUID) -> ProjectSkill | None:
    stmt = (
        select(ProjectSkill)
        .where(ProjectSkill.id == enable_id)
        .options(selectinload(ProjectSkill.skill))
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def update_project_skill(
    session: AsyncSession, row: ProjectSkill, data: ProjectSkillUpdate
) -> ProjectSkill:
    if data.enabled is not None:
        row.enabled = data.enabled
    if data.config is not None:
        row.config = data.config
    if data.sort_order is not None:
        row.sort_order = data.sort_order
    if data.unpin_version:
        row.version_id = None
    elif data.version_id is not None:
        row.version_id = data.version_id
    await session.commit()
    await session.refresh(row)
    return row


async def delete_project_skill(session: AsyncSession, row: ProjectSkill) -> None:
    await session.delete(row)
    await session.commit()


# ---- Voyage 快照（§3.2）----


async def snapshot_for_project(
    session: AsyncSession, project_id: uuid.UUID
) -> dict[str, list[dict[str, Any]]]:
    """项目生效技能 → checkpoint["skills"]：{target: [条目...]}，条目含完整 body。

    - pin 了版本用 pin 版本，否则用最新版本；
    - 归档技能 / disabled 行跳过；
    - 结果自包含（不依赖 DB），保证断点恢复与审计回放。
    """
    rows = await list_project_skills(session, project_id)
    snapshot: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        if not row.enabled or row.skill is None or row.skill.is_archived:
            continue
        version: SkillVersion | None = None
        if row.version_id is not None:
            version = await session.get(SkillVersion, row.version_id)
        if version is None:
            version = await latest_version(session, row.skill_id)
        if version is None:
            continue
        snapshot.setdefault(row.target, []).append(
            {
                "skill_id": str(row.skill_id),
                "slug": row.skill.slug,
                "name": row.skill.name,
                "kind": row.skill.kind,
                "version": version.version,
                "body": version.body,
                "config": row.config or {},
                "personas": (version.manifest or {}).get("personas") or [],
                "steps": (version.manifest or {}).get("steps") or [],
                "output_contract": (version.manifest or {}).get("output_contract"),
            }
        )
    return snapshot


# ---- 试运行（docs/skill-system.md §4.1）----


def _snapshot_entry(skill: Skill, version: SkillVersion) -> dict[str, Any]:
    return {
        "skill_id": str(skill.id),
        "slug": skill.slug,
        "name": skill.name,
        "kind": skill.kind,
        "version": version.version,
        "body": version.body,
        "config": {},
        "personas": (version.manifest or {}).get("personas") or [],
        "steps": (version.manifest or {}).get("steps") or [],
        "output_contract": (version.manifest or {}).get("output_contract"),
    }


async def test_run_skill(
    session: AsyncSession,
    skill: Skill,
    *,
    user_id: uuid.UUID,
    goal: str,
    target: str | None = None,
) -> dict[str, Any]:
    """预览技能效果：guidance/rubric 渲染注入文本并真实调用一次 LLM（stage=default，
    无路由时回退 fake）；persona/workflow 只返回结构预览，不调 LLM。"""
    version = await latest_version(session, skill.id)
    if version is None:
        raise SkillReadOnlyError(f"{skill.slug} has no version")
    entry = _snapshot_entry(skill, version)

    if skill.kind == "persona":
        rendered = json.dumps(entry["personas"], ensure_ascii=False, indent=2)
        return {"rendered": rendered, "output": None, "model": None}
    if skill.kind == "workflow":
        rendered = json.dumps(entry["steps"], ensure_ascii=False, indent=2)
        return {"rendered": rendered, "output": None, "model": None}

    from app.agents.voyage.skillset import skill_guidance
    from app.core.llm.base import Message
    from app.core.llm.router import get_llm_router

    resolved_target = target or next(iter(_skill_targets(version.manifest)), "preview")
    rendered = skill_guidance({"skills": {resolved_target: [entry]}}, resolved_target)
    system = "你是自动科研平台某个环节的执行者，请严格按补充判断标准处理用户输入。" + rendered
    result = await get_llm_router().complete(
        "default",
        [Message(role="system", content=system), Message(role="user", content=goal)],
        user_id=user_id,
    )
    return {"rendered": rendered, "output": result.content, "model": result.model}


# ---- 运行 workflow 技能（docs/skill-system.md §3.3）----


async def run_workflow_skill(
    session: AsyncSession,
    skill: Skill,
    *,
    project_id: uuid.UUID,
    created_by: uuid.UUID,
    goal: str,
    run_vars: dict[str, str] | None = None,
    budget: dict[str, Any] | None = None,
) -> "VoyageRun":
    """以 workflow 技能 steps 为计划直接创建 voyage（kind=custom，跳过 Navigator 规划）。

    run_vars 合并进每个步骤的 params.vars，供 prompt 模板渲染（{goal} 始终可用）。
    """
    if skill.kind != "workflow":
        raise SkillWorkflowInvalidError(f"{skill.slug} is not a workflow skill")
    version = await latest_version(session, skill.id)
    steps = (version.manifest or {}).get("steps") if version else None
    if not steps:
        raise SkillWorkflowInvalidError(f"{skill.slug} has no steps")
    # 保存时校验过，这里再过一遍防旧数据/动作下线
    import app.agents.voyage  # noqa: F401
    from app.agents.voyage.navigator import validate_steps

    try:
        plan = validate_steps({"steps": steps})
    except ValueError as e:
        raise SkillWorkflowInvalidError(str(e)) from e
    if run_vars:
        for step in plan:
            params = dict(step.get("params") or {})
            params["vars"] = {**(params.get("vars") or {}), **run_vars}
            step["params"] = params

    from app.models.voyage import VoyageRun

    run = VoyageRun(
        kind="custom",
        goal=goal,
        status="planning",
        plan=plan,
        cursor=0,
        checkpoint={
            "params": {
                "skill_slug": skill.slug,
                "skill_version": version.version if version else None,
            }
        },
        budget=budget,
        project_id=project_id,
        created_by=created_by,
    )
    session.add(run)
    await session.commit()
    await session.refresh(run)
    return run


# ---- 内置种子 ----


async def ensure_builtin_skills(session: AsyncSession) -> int:
    """按 slug 幂等插入内置技能，返回新插入数量。已存在不覆盖。"""
    existing = set(
        (await session.execute(select(Skill.slug).where(Skill.scope == "builtin"))).scalars()
    )
    created = 0
    for seed in BUILTIN_SKILLS:
        if seed["slug"] in existing:
            continue
        manifest = SkillManifest(
            targets=seed["targets"],
            personas=seed.get("personas") or [],
            steps=seed.get("steps") or [],
        )
        skill = Skill(
            slug=seed["slug"],
            kind=seed["kind"],
            name=seed["name"],
            name_en=seed.get("name_en"),
            description=seed.get("description"),
            scope="builtin",
            owner_id=None,
        )
        session.add(skill)
        await session.flush()
        session.add(
            SkillVersion(
                skill_id=skill.id,
                version=1,
                manifest=manifest.model_dump(mode="json"),
                body=seed["body"],
            )
        )
        created += 1
    if created:
        await session.commit()
    return created


async def builtin_count(session: AsyncSession) -> int:
    stmt = select(func.count()).select_from(Skill).where(Skill.scope == "builtin")
    return int((await session.execute(stmt)).scalar_one())
