"""Idea Forge / 评审锦标赛业务逻辑（不 import fastapi）。"""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.activity import Activity
from app.models.gate import Gate
from app.models.idea import IDEA_STATUSES, Idea
from app.models.paper import Concept, Paper
from app.models.project import Project, ProjectMember
from app.models.user import User
from app.models.voyage import TERMINAL_STATUSES, VoyageRun
from app.schemas.idea import DeepIdeaRequest, ForgeKnobs
from app.schemas.review import TournamentRequest

# 同项目 idea 类 voyage 互斥（docs/api-m3.md §1 + docs/api-idea2.md §2）
IDEA_VOYAGE_KINDS = ("idea_forge", "idea_review", "idea_proposal")

# 预算从 knobs 派生：每个候选 idea 预留的 token 额度（gap 分析+生成+打分+去重）
_TOKENS_PER_IDEA = 20_000
# 每场辩论每次 LLM 调用预留：辩论上下文逐轮累积（双方发言+人设+历史），裁判看全场，
# 思考型模型下单次可达 15-20k，估低会在汇总前触发预算门（改由 §5.4 降级收尾兜底）
_TOKENS_PER_MATCH_CALL = 16_000
# 深耕 voyage 默认预算（目标构建工具循环 + 各节起草 + 评审修订）
_DEEP_DEFAULT_BUDGET = 400_000

IDEA_SORTS = ("elo", "-created_at", "score")

# 深耕相关闸门（docs/api-idea2.md §4/§5）
DEEP_GATE_KINDS = ("idea_goal", "idea_pivot")


class IdeaVoyageConflictError(Exception):
    """同一项目已有 idea 类 voyage 在跑。"""


class NotEnoughIdeasError(Exception):
    """锦标赛参与 idea 不足两个。"""


class InvalidIdeaIdsError(Exception):
    """显式 idea_ids 含不存在/不属于本项目的 id。"""


class InvalidSeedError(Exception):
    """深耕种子引用的 concept/paper/idea 不存在或不属于本项目。"""


# ---- voyage 创建 ----


async def find_running_idea_voyage(
    session: AsyncSession, project_id: uuid.UUID
) -> VoyageRun | None:
    stmt = (
        select(VoyageRun)
        .where(
            VoyageRun.project_id == project_id,
            VoyageRun.kind.in_(IDEA_VOYAGE_KINDS),
            VoyageRun.status.not_in(tuple(TERMINAL_STATUSES)),
        )
        .order_by(VoyageRun.created_at.desc())
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def create_forge_voyage(
    session: AsyncSession,
    *,
    project: Project,
    knobs: ForgeKnobs,
    created_by: uuid.UUID | None,
) -> VoyageRun:
    """建 idea_forge voyage（forge/review 互斥 + Activity 落记录），由调用方入队 run_voyage。"""
    if await find_running_idea_voyage(session, project.id) is not None:
        raise IdeaVoyageConflictError(str(project.id))
    run = VoyageRun(
        kind="idea_forge",
        goal=f"Idea Forge：{project.name}",
        status="planning",
        cursor=0,
        checkpoint={"params": {"knobs": knobs.model_dump()}},
        budget={"max_tokens": int(knobs.num_ideas) * _TOKENS_PER_IDEA},
        project_id=project.id,
        created_by=created_by,
    )
    session.add(run)
    session.add(
        Activity(
            project_id=project.id,
            actor=f"user:{created_by}" if created_by else "system",
            kind="forge.started",
            message=f"Idea Forge 已启动（目标 {knobs.num_ideas} 个候选）",
            payload={"knobs": knobs.model_dump()},
        )
    )
    await session.commit()
    await session.refresh(run)
    return run


async def create_tournament_voyage(
    session: AsyncSession,
    *,
    project: Project,
    data: TournamentRequest,
    created_by: uuid.UUID | None,
) -> VoyageRun:
    """建 idea_review（辩论锦标赛）voyage；参与者不足 2 个抛 NotEnoughIdeasError。"""
    if await find_running_idea_voyage(session, project.id) is not None:
        raise IdeaVoyageConflictError(str(project.id))

    if data.idea_ids:
        wanted = list(dict.fromkeys(data.idea_ids))
        stmt = select(Idea.id).where(Idea.project_id == project.id, Idea.id.in_(wanted))
        found = {row for (row,) in (await session.execute(stmt)).all()}
        missing = [str(i) for i in wanted if i not in found]
        if missing:
            raise InvalidIdeaIdsError(", ".join(missing))
        participant_count = len(wanted)
    else:
        stmt = select(func.count()).where(
            Idea.project_id == project.id, Idea.status.in_(("candidate", "under_review"))
        )
        participant_count = int((await session.execute(stmt)).scalar_one())
    if participant_count < 2:
        raise NotEnoughIdeasError(str(project.id))

    matches = participant_count // 2
    params: dict[str, Any] = {
        "idea_ids": [str(i) for i in data.idea_ids] if data.idea_ids else None,
        "rounds": data.rounds,
        "personas": [p.model_dump() for p in data.personas] if data.personas else None,
    }
    run = VoyageRun(
        kind="idea_review",
        goal=f"Idea 评审锦标赛：{project.name}",
        status="planning",
        cursor=0,
        checkpoint={"params": params},
        budget={"max_tokens": matches * (2 * data.rounds + 1) * _TOKENS_PER_MATCH_CALL},
        project_id=project.id,
        created_by=created_by,
    )
    session.add(run)
    session.add(
        Activity(
            project_id=project.id,
            actor=f"user:{created_by}" if created_by else "system",
            kind="review.started",
            message=f"Idea 评审锦标赛已启动（{participant_count} 个想法）",
            payload=params,
        )
    )
    await session.commit()
    await session.refresh(run)
    return run


async def _validate_seed(
    session: AsyncSession, *, project_id: uuid.UUID, seed_type: str, value: str
) -> str:
    """引用型种子存在性校验，返回种子摘要（写入 voyage goal 文案）。"""
    if seed_type == "text":
        return value[:80]
    try:
        target_id = uuid.UUID(value)
    except ValueError as e:
        raise InvalidSeedError(value) from e
    if seed_type == "paper":
        paper = await session.get(Paper, target_id)
        if paper is None or paper.project_id != project_id:
            raise InvalidSeedError(value)
        return f"论文《{paper.title[:60]}》"
    if seed_type == "concept":
        concept = await session.get(Concept, target_id)
        if concept is None or concept.project_id != project_id:
            raise InvalidSeedError(value)
        return f"概念「{concept.name}」"
    if seed_type == "idea":
        idea = await session.get(Idea, target_id)
        if idea is None or idea.project_id != project_id:
            raise InvalidSeedError(value)
        return f"草案「{idea.title[:60]}」"
    raise InvalidSeedError(seed_type)


async def create_deep_voyage(
    session: AsyncSession,
    *,
    project: Project,
    data: DeepIdeaRequest,
    created_by: uuid.UUID | None,
) -> VoyageRun:
    """建 idea_proposal voyage（深度生成，docs/api-idea2.md §2），由调用方入队 run_voyage。"""
    if await find_running_idea_voyage(session, project.id) is not None:
        raise IdeaVoyageConflictError(str(project.id))
    seed_brief = await _validate_seed(
        session, project_id=project.id, seed_type=data.seed.type, value=data.seed.value
    )
    budget = data.knobs.budget_tokens or _DEEP_DEFAULT_BUDGET
    run = VoyageRun(
        kind="idea_proposal",
        goal=f"深度研究方案：{project.name}",
        status="planning",
        cursor=0,
        checkpoint={"params": {"seed": data.seed.model_dump(), "knobs": data.knobs.model_dump()}},
        budget={"max_tokens": budget},
        project_id=project.id,
        created_by=created_by,
    )
    session.add(run)
    session.add(
        Activity(
            project_id=project.id,
            actor=f"user:{created_by}" if created_by else "system",
            kind="idea.deep_started",
            message=f"深度想法生成已启动（种子：{seed_brief}）",
            payload={"seed": data.seed.model_dump(), "knobs": data.knobs.model_dump()},
        )
    )
    await session.commit()
    await session.refresh(run)
    return run


async def deep_state(session: AsyncSession, project: Project) -> dict[str, Any]:
    """深度生成状态（docs/api-idea2.md §2）：运行中 voyage + 待审批闸门 + 上次运行。"""
    running_stmt = (
        select(VoyageRun)
        .where(
            VoyageRun.project_id == project.id,
            VoyageRun.kind == "idea_proposal",
            VoyageRun.status.not_in(tuple(TERMINAL_STATUSES)),
        )
        .order_by(VoyageRun.created_at.desc())
        .limit(1)
    )
    running = (await session.execute(running_stmt)).scalar_one_or_none()

    pending_gate_id: uuid.UUID | None = None
    if running is not None:
        gate_stmt = (
            select(Gate)
            .where(
                Gate.project_id == project.id,
                Gate.kind.in_(DEEP_GATE_KINDS),
                Gate.status == "pending",
                Gate.payload["voyage_id"].as_string() == str(running.id),
            )
            .order_by(Gate.created_at.desc())
            .limit(1)
        )
        gate = (await session.execute(gate_stmt)).scalars().first()
        pending_gate_id = gate.id if gate is not None else None

    last_stmt = (
        select(VoyageRun)
        .where(VoyageRun.project_id == project.id, VoyageRun.kind == "idea_proposal")
        .order_by(VoyageRun.created_at.desc())
        .limit(1)
    )
    last = (await session.execute(last_stmt)).scalar_one_or_none()
    last_run: dict[str, Any] | None = None
    if last is not None:
        last_run = {
            "voyage_id": last.id,
            "status": last.status,
            "finished_at": last.updated_at if last.status in TERMINAL_STATUSES else None,
        }
    return {
        "running_voyage_id": running.id if running else None,
        "pending_gate_id": pending_gate_id,
        "last_run": last_run,
    }


# ---- forge 状态 ----


async def idea_counts(session: AsyncSession, project_id: uuid.UUID) -> dict[str, int]:
    rows = (
        await session.execute(
            select(Idea.status, func.count())
            .where(Idea.project_id == project_id)
            .group_by(Idea.status)
        )
    ).all()
    counts = {status: 0 for status in IDEA_STATUSES}
    total = 0
    for status, count in rows:
        counts[status] = int(count)
        total += int(count)
    counts["total"] = total
    return counts


async def forge_state(session: AsyncSession, project: Project) -> dict[str, Any]:
    running = await find_running_idea_voyage(session, project.id)
    stmt = (
        select(VoyageRun)
        .where(VoyageRun.project_id == project.id, VoyageRun.kind == "idea_forge")
        .order_by(VoyageRun.created_at.desc())
        .limit(1)
    )
    last = (await session.execute(stmt)).scalar_one_or_none()
    last_run: dict[str, Any] | None = None
    if last is not None:
        last_run = {
            "voyage_id": last.id,
            "status": last.status,
            "finished_at": last.updated_at if last.status in TERMINAL_STATUSES else None,
        }
    return {
        "running_voyage_id": running.id if running else None,
        "last_run": last_run,
        "idea_counts": await idea_counts(session, project.id),
    }


# ---- idea 读写 ----


def composite_score(idea: Idea) -> float:
    scores = idea.scores if isinstance(idea.scores, dict) else {}
    values = [float(v) for v in scores.values() if isinstance(v, int | float)]
    return sum(values) / len(values) if values else -1.0


async def list_ideas(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    status: str | None = None,
    depth: str | None = None,
    research_type: str | None = None,
    sort: str = "-created_at",
    trashed: bool = False,
) -> list[Idea]:
    trash_cond = Idea.trashed_at.is_not(None) if trashed else Idea.trashed_at.is_(None)
    stmt = select(Idea).where(Idea.project_id == project_id, trash_cond)
    if trashed:
        return list((await session.execute(stmt.order_by(Idea.trashed_at.desc()))).scalars().all())
    if status:
        stmt = stmt.where(Idea.status == status)
    if depth:
        stmt = stmt.where(Idea.depth == depth)
    if research_type:
        stmt = stmt.where(Idea.research_type == research_type)
    if sort == "elo":
        stmt = stmt.order_by(Idea.elo_rating.desc(), Idea.created_at.desc())
    else:
        stmt = stmt.order_by(Idea.created_at.desc())
    ideas = list((await session.execute(stmt)).scalars().all())
    if sort == "score":  # composite 存 JSON，跨方言排序在 Python 侧做
        ideas.sort(key=composite_score, reverse=True)
    return ideas


async def get_idea_for_user(
    session: AsyncSession, *, idea_id: uuid.UUID, user_id: uuid.UUID
) -> Idea | None:
    """取 idea；非项目成员视为不存在。"""
    stmt = (
        select(Idea)
        .join(ProjectMember, ProjectMember.project_id == Idea.project_id)
        .where(Idea.id == idea_id, ProjectMember.user_id == user_id)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def _owned_ideas(
    session: AsyncSession, *, project_id: uuid.UUID, ids: list[uuid.UUID]
) -> list[Idea]:
    if not ids:
        return []
    stmt = select(Idea).where(Idea.project_id == project_id, Idea.id.in_(ids))
    return list((await session.execute(stmt)).scalars().all())


async def trash_ideas(session: AsyncSession, *, project_id: uuid.UUID, ids: list[uuid.UUID]) -> int:
    """移入垃圾箱（软删除）；返回受影响数量。"""
    now = datetime.now(UTC)
    n = 0
    for idea in await _owned_ideas(session, project_id=project_id, ids=ids):
        if idea.trashed_at is None:
            idea.trashed_at = now
            n += 1
    await session.commit()
    return n


async def restore_ideas(
    session: AsyncSession, *, project_id: uuid.UUID, ids: list[uuid.UUID]
) -> int:
    n = 0
    for idea in await _owned_ideas(session, project_id=project_id, ids=ids):
        if idea.trashed_at is not None:
            idea.trashed_at = None
            n += 1
    await session.commit()
    return n


async def purge_ideas(
    session: AsyncSession, *, project_id: uuid.UUID, ids: list[uuid.UUID] | None = None
) -> int:
    """永久删除。ids=None → 清空该项目垃圾箱；否则只删指定 id 中已在垃圾箱的。
    级联删除其实验（DB FK ondelete=CASCADE）。返回删除数量。"""
    if ids is None:
        rows = await list_ideas(session, project_id=project_id, trashed=True)
    else:
        rows = [
            i
            for i in await _owned_ideas(session, project_id=project_id, ids=ids)
            if i.trashed_at is not None
        ]
    n = len(rows)
    for idea in rows:
        await session.delete(idea)
    await session.commit()
    return n


async def set_idea_status(session: AsyncSession, idea: Idea, status: str) -> Idea:
    idea.status = status
    await session.commit()
    await session.refresh(idea)
    return idea


def parse_parent_ids(idea: Idea) -> list[uuid.UUID]:
    """idea.parent_paper_ids（JSON 字符串列表）→ UUID 列表（无效 id 静默忽略）。"""
    ids: list[uuid.UUID] = []
    for raw in idea.parent_paper_ids or []:
        try:
            ids.append(uuid.UUID(str(raw)))
        except ValueError:
            continue
    return ids


async def parent_papers_brief(session: AsyncSession, idea: Idea) -> list[dict[str, Any]]:
    """IdeaDetail.parent_papers：{id, title}（已删除的论文静默忽略）。"""
    ids = parse_parent_ids(idea)
    if not ids:
        return []
    rows = (await session.execute(select(Paper.id, Paper.title).where(Paper.id.in_(ids)))).all()
    by_id = {pid: title for pid, title in rows}
    return [{"id": pid, "title": by_id[pid]} for pid in ids if pid in by_id]


async def seed_idea_brief(session: AsyncSession, idea: Idea) -> dict[str, Any] | None:
    """IdeaDetail.seed_idea：深化来源草案 {id, title}（已删除静默为 None）。"""
    if idea.seed_idea_id is None:
        return None
    seed = await session.get(Idea, idea.seed_idea_id)
    return {"id": seed.id, "title": seed.title} if seed is not None else None


# ---- 晋级（idea_promotion 闸门） ----


async def can_promote(session: AsyncSession, *, project_id: uuid.UUID, user: User) -> bool:
    """promote 权限：项目 owner（成员角色 owner）或平台 admin。"""
    if user.role == "admin":
        return True
    member = await session.get(ProjectMember, (project_id, user.id))
    return member is not None and member.role == "owner"


async def find_pending_promotion_gate(session: AsyncSession, idea_id: uuid.UUID) -> Gate | None:
    stmt = select(Gate).where(
        Gate.kind == "idea_promotion",
        Gate.status == "pending",
        Gate.payload["idea_id"].as_string() == str(idea_id),
    )
    return (await session.execute(stmt)).scalars().first()


async def create_promotion_gate(session: AsyncSession, idea: Idea, user: User) -> Gate:
    gate = Gate(
        project_id=idea.project_id,
        kind="idea_promotion",
        payload={"idea_id": str(idea.id), "idea_title": idea.title},
        requested_by=f"user:{user.id}",
    )
    session.add(gate)
    session.add(
        Activity(
            project_id=idea.project_id,
            actor=f"user:{user.id}",
            kind="idea.promote_requested",
            message=f"想法「{idea.title}」申请晋级，等待人工审批",
            payload={"idea_id": str(idea.id), "gate_id": None},
        )
    )
    await session.commit()
    await session.refresh(gate)
    return gate


async def promote_from_gate(session: AsyncSession, gate: Gate) -> Idea | None:
    """gates approve 联动：payload.idea_id 存在则把 idea 置为 promoted，返回该 idea。"""
    raw = (gate.payload or {}).get("idea_id")
    if not raw:
        return None
    try:
        idea_id = uuid.UUID(str(raw))
    except ValueError:
        return None
    idea = await session.get(Idea, idea_id)
    if idea is None:
        return None
    if idea.status != "promoted":
        idea.status = "promoted"
        session.add(
            Activity(
                project_id=idea.project_id,
                actor=f"user:{gate.decided_by}" if gate.decided_by else "system",
                kind="idea.promoted",
                message=f"想法「{idea.title}」已通过审批晋级",
                payload={"idea_id": str(idea.id), "gate_id": str(gate.id)},
            )
        )
        await session.commit()
        await session.refresh(idea)
    return idea
