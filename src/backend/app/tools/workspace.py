"""课题工作区只读工具：总览、AI 任务、人工闸门。

外部 harness（Claude Code / Codex）接进来第一件事是「这个课题现在是什么状态、
有什么在跑、卡在哪」。这三件事此前只有网页端看得到，MCP 层完全没有——
本模块把它们补上，全部只读。

任务（voyage）与闸门（gate）都按课题隔离：任务必须属于本课题，或属于本课题
关联的文献库（建库/增量抓取挂在库上，但那正是课题语料的来源，harness 需要看得到）。
"""

from __future__ import annotations

import uuid
from typing import Any

from app.core.db import get_sessionmaker
from app.models.voyage import IN_FLIGHT_STATUSES, TERMINAL_STATUSES, VoyageRun
from app.services import gates as gates_service
from app.services import stats as stats_service
from app.services import voyage_logs as voyage_logs_service
from app.services import voyages as voyages_service
from app.services.libraries import get_source_libraries
from app.tools.context import ToolContext
from app.tools.registry import tool

_MAX_TASKS = 50
_MAX_LOG_LINES = 500
_LOG_CHARS = 2000  # 单条日志上限（大模型完整输出可能极长）
_GOAL_CHARS = 300
_MAX_GATES = 50

_PAUSED_STATUSES = frozenset({"paused_gate", "paused_error", "paused_ask"})


def _task_brief(run: VoyageRun) -> dict[str, Any]:
    return {
        "task_id": str(run.id),
        "kind": run.kind,
        "status": run.status,
        "goal": (run.goal or "")[:_GOAL_CHARS],
        "created_at": run.created_at,
        "scope": "library" if run.project_id is None else "topic",
    }


def _matches_status(run: VoyageRun, want: str) -> bool:
    if want == "running":
        return run.status in IN_FLIGHT_STATUSES
    if want == "paused":
        return run.status in _PAUSED_STATUSES
    if want == "done":
        return run.status in TERMINAL_STATUSES
    return True


async def _visible_tasks(session: Any, ctx: ToolContext) -> list[VoyageRun]:
    """本课题够得着的任务：课题自己的 + 关联文献库的（建库/抓取）。

    ``list_voyages`` 已按用户可见性过滤；这里再按课题作用域收一次，避免把用户
    在别的课题里的任务漏进本次 MCP 会话。
    """
    libraries = await get_source_libraries(session, ctx.project_id)
    library_ids = {lib.id for lib in libraries}
    runs = await voyages_service.list_voyages(session, user_id=ctx.user_id)
    return [
        run
        for run in runs
        if run.project_id == ctx.project_id
        or (run.library_id is not None and run.library_id in library_ids)
    ]


async def _get_task(session: Any, ctx: ToolContext, raw_id: Any) -> VoyageRun:
    try:
        task_id = uuid.UUID(str(raw_id))
    except (ValueError, TypeError) as e:
        raise ValueError(f"task_id 不是合法 uuid：{raw_id}") from e
    run = await voyages_service.get_voyage(
        session, voyage_id=task_id, user_id=ctx.user_id, with_steps=True
    )
    if run is None:
        raise ValueError(f"任务不存在或无权访问：{raw_id}")
    libraries = await get_source_libraries(session, ctx.project_id)
    library_ids = {lib.id for lib in libraries}
    in_scope = run.project_id == ctx.project_id or (
        run.library_id is not None and run.library_id in library_ids
    )
    if not in_scope:
        raise ValueError(f"该任务不属于本课题：{raw_id}")
    return run


@tool(
    "get_project_status",
    description="取本课题总览，含论文、想法、实验与稿件计数，待审闸门数量，关联文献库，进行中的任务与最近动态",
    input_schema={"type": "object", "properties": {}},
    summarize=lambda a, r: f"课题总览（{r.get('papers_total', 0)} 篇论文）",
)
async def get_project_status(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    async with get_sessionmaker()() as session:
        stats = await stats_service.project_stats(session, ctx.project_id)
        libraries = await get_source_libraries(session, ctx.project_id)
        runs = await _visible_tasks(session, ctx)
    active = [r for r in runs if r.status not in TERMINAL_STATUSES]
    return {
        **{k: v for k, v in stats.items() if k != "recent_activities"},
        "source_libraries": [{"library_id": str(lib.id), "name": lib.name} for lib in libraries],
        "active_tasks": [_task_brief(r) for r in active[:10]],
        "recent_activities": [
            {"kind": a["kind"], "message": a["message"], "created_at": a["created_at"]}
            for a in (stats.get("recent_activities") or [])[:10]
        ],
    }


@tool(
    "list_tasks",
    description="列出本课题的 AI 任务，按创建时间由新到旧排列，可按状态与类型筛选",
    input_schema={
        "type": "object",
        "properties": {
            "status": {
                "type": "string",
                "enum": ["all", "running", "paused", "done"],
                "description": "默认 all；paused = 等人工审批或出错待重试",
            },
            "kind": {"type": "string", "description": "只看某一类任务（见返回里的 kind）"},
            "limit": {"type": "integer", "minimum": 1, "maximum": _MAX_TASKS, "default": 20},
        },
    },
    summarize=lambda a, r: f"任务清单（{len(r.get('tasks') or [])} 条）",
)
async def list_tasks(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    want = str(args.get("status") or "all")
    kind = str(args.get("kind") or "").strip() or None
    limit = min(_MAX_TASKS, max(1, int(args.get("limit") or 20)))
    async with get_sessionmaker()() as session:
        runs = await _visible_tasks(session, ctx)
    picked = [r for r in runs if _matches_status(r, want) and (kind is None or r.kind == kind)]
    return {
        "total": len(picked),
        "tasks": [_task_brief(r) for r in picked[:limit]],
    }


def _blocked_on(run: VoyageRun, gates: list[Any]) -> dict[str, Any] | None:
    """任务停在人工闸门时，把该闸门抬到顶层——否则 harness 不知道该批准什么。"""
    if run.status != "paused_gate":
        return None
    for gate in gates:
        if gates_service.gate_voyage_id(gate) == run.id:
            return {
                "gate_id": str(gate.id),
                "kind": gate.kind,
                "payload": gate.payload,
                "next_action": "本课题成员可在网页端审批该闸门后任务会自动继续",
            }
    return None


def _last_error(run: VoyageRun) -> str | None:
    """paused_error / failed 时给出最后一个失败步骤的原因。"""
    if run.status not in ("paused_error", "failed"):
        return None
    for step in sorted(run.steps, key=lambda s: s.rank):
        if step.status == "failed":
            verdict = step.verdict or {}
            observation = step.observation or {}
            reason = verdict.get("reason") or observation.get("error")
            if reason:
                return f"{step.title}：{str(reason)[:500]}"
    return None


@tool(
    "get_task",
    description="取某个任务的详情，含状态、计划步骤与判定结果、正在阻塞它的审批闸门、失败原因与产出物标识",
    input_schema={
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "任务 uuid（见 list_tasks）"},
            "include_steps": {"type": "boolean", "description": "是否返回步骤明细，默认 true"},
        },
        "required": ["task_id"],
    },
    summarize=lambda a, r: f"任务详情：{r.get('kind', '')} / {r.get('status', '')}",
)
async def get_task(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    include_steps = args.get("include_steps", True) is not False
    async with get_sessionmaker()() as session:
        run = await _get_task(session, ctx, args.get("task_id"))
        gates = list(
            await gates_service.list_gates(
                session, user_id=ctx.user_id, status="pending", project_id=ctx.project_id
            )
        )
        checkpoint = run.checkpoint or {}
        payload: dict[str, Any] = {
            "task_id": str(run.id),
            "kind": run.kind,
            "mode": run.mode,
            "status": run.status,
            "goal": run.goal,
            "cursor": run.cursor,
            "plan_iteration": run.plan_iteration,
            "usage": run.usage,
            "budget": run.budget,
            "created_at": run.created_at,
            "blocked_on": _blocked_on(run, gates),
            "last_error": _last_error(run),
            "artifacts": checkpoint.get("params") or {},
        }
        if include_steps:
            payload["steps"] = [
                {
                    "seq": step.seq,
                    "title": step.title,
                    "action": step.action,
                    "status": step.status,
                    "requires_gate": step.requires_gate,
                    "verdict": step.verdict,
                }
                for step in sorted(run.steps, key=lambda s: s.rank)
                if step.status != "obsolete"
            ]
        return payload


@tool(
    "read_task_logs",
    description="读取某个任务的终端日志，含结构化日志行与模型输出，按时间正序返回最近若干条",
    input_schema={
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "任务 uuid"},
            "limit": {"type": "integer", "minimum": 1, "maximum": _MAX_LOG_LINES, "default": 100},
            "level": {"type": "string", "description": "只看某个级别（如 error / gate）"},
        },
        "required": ["task_id"],
    },
    summarize=lambda a, r: f"任务日志（{len(r.get('logs') or [])} 条）",
)
async def read_task_logs(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    limit = min(_MAX_LOG_LINES, max(1, int(args.get("limit") or 100)))
    level = str(args.get("level") or "").strip() or None
    async with get_sessionmaker()() as session:
        run = await _get_task(session, ctx, args.get("task_id"))
        # 先按 level 过滤会拿不满 limit，这里多取一些再筛（日志本身已按 id 尾部截断）
        rows = await voyage_logs_service.fetch_terminal_logs(
            session, run.id, limit=limit if level is None else limit * 4
        )
    picked = [r for r in rows if level is None or r.level == level][-limit:]
    return {
        "task_id": str(run.id),
        "status": run.status,
        "logs": [
            {
                "id": row.id,
                "event": row.event,
                "level": row.level,
                "stage": row.stage,
                "at": row.at,
                "message": (row.message or "")[:_LOG_CHARS],
            }
            for row in picked
        ],
    }


@tool(
    "list_gates",
    description="列出本课题的人工审批闸门，含想法晋级、算力预算与投稿等类型；审批操作需在网页端完成",
    input_schema={
        "type": "object",
        "properties": {
            "status": {
                "type": "string",
                "enum": ["pending", "decided", "all"],
                "description": "默认 pending",
            },
            "limit": {"type": "integer", "minimum": 1, "maximum": _MAX_GATES, "default": 20},
        },
    },
    summarize=lambda a, r: f"闸门清单（{len(r.get('gates') or [])} 个）",
)
async def list_gates(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    status = str(args.get("status") or "pending")
    limit = min(_MAX_GATES, max(1, int(args.get("limit") or 20)))
    async with get_sessionmaker()() as session:
        rows = await gates_service.list_gates(
            session,
            user_id=ctx.user_id,
            status=None if status == "all" else status,
            project_id=ctx.project_id,
        )
    return {
        "total": len(rows),
        "gates": [
            {
                "gate_id": str(gate.id),
                "kind": gate.kind,
                "status": gate.status,
                "payload": gate.payload,
                "requested_by": gate.requested_by,
                "comment": gate.comment,
                "created_at": gate.created_at,
                "task_id": (
                    str(vid) if (vid := gates_service.gate_voyage_id(gate)) is not None else None
                ),
            }
            for gate in rows[:limit]
        ],
    }
