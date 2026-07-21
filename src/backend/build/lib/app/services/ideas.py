"""Idea Forge / 评审锦标赛业务逻辑（不 import fastapi）。"""

import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.activity import Activity
from app.models.gate import Gate
from app.models.idea import IDEA_STATUSES, Idea
from app.models.paper import Paper
from app.models.project import Project, ProjectMember
from app.models.user import User
from app.models.voyage import TERMINAL_STATUSES, VoyageRun
from app.schemas.idea import ForgeKnobs
from app.schemas.review import TournamentRequest

# 同项目 forge/review 互斥（docs/api-m3.md §1）
IDEA_VOYAGE_KINDS = ("idea_forge", "idea_review")

# 预算从 knobs 派生：每个候选 idea 预留的 token 额度（gap 分析+生成+打分+去重）
_TOKENS_PER_IDEA = 20_000
# 每场辩论预留：正/反方每轮各一次 + 裁判一次
_TOKENS_PER_MATCH_CALL = 8_000

IDEA_SORTS = ("elo", "-created_at", "score")


class IdeaVoyageConflictError(Exception):
    """同一项目已有 forge/review voyage 在跑。"""


class NotEnoughIdeasError(Exception):
    """锦标赛参与 idea 不足两个。"""


class InvalidIdeaIdsError(Exception):
    """显式 idea_ids 含不存在/不属于本项目的 id。"""


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
        goal=f"Idea Forge：{project.name}（生成 {knobs.num_ideas} 个候选想法）",
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
        goal=(
            f"Idea 评审锦标赛：{project.name}"
            f"（{participant_count} 个想法，每场 {data.rounds} 轮辩论）"
        ),
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
    sort: str = "-created_at",
) -> list[Idea]:
    stmt = select(Idea).where(Idea.project_id == project_id)
    if status:
        stmt = stmt.where(Idea.status == status)
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
