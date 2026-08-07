"""项目状态只读工具：想法 / 实验 / 稿件 fact-pack —— 供评审/写作/实验 agent 取项目态。"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.core.db import get_sessionmaker
from app.models.experiment import Experiment
from app.models.idea import Idea
from app.models.manuscript import Manuscript
from app.services import experiments as experiments_service
from app.services import ideas as ideas_service
from app.services import manuscripts as manuscripts_service
from app.tools.context import ToolContext
from app.tools.registry import tool
from app.tools.scope import project_ids_for

_CONTENT_CHARS = 4000
_MAX_LOG_LINES = 1000


def _idea_brief(idea: Idea) -> dict[str, Any]:
    return {
        "idea_id": str(idea.id),
        "title": idea.title,
        "summary": idea.summary,
        "status": idea.status,
        "depth": idea.depth,
        "research_type": idea.research_type,
        "elo_rating": round(idea.elo_rating, 1),
        "scores": idea.scores,
    }


@tool(
    "list_ideas",
    description="列出本课题的想法，可按状态、深度与研究类型筛选",
    input_schema={
        "type": "object",
        "properties": {
            "status": {"type": "string"},
            "depth": {"type": "string", "enum": ["sketch", "deep"]},
            "research_type": {"type": "string"},
        },
    },
    summarize=lambda a, r: f"想法清单（{len(r.get('ideas') or [])} 条）",
)
async def list_ideas(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    async with get_sessionmaker()() as session:
        ideas = await ideas_service.list_ideas(
            session,
            project_ids=await project_ids_for(session, ctx),
            status=str(args.get("status") or "") or None,
            depth=str(args.get("depth") or "") or None,
            research_type=str(args.get("research_type") or "") or None,
        )
    return {"ideas": [_idea_brief(i) for i in ideas[:50]]}


@tool(
    "get_idea",
    description="取某个想法的完整内容，含动机、方法、预期实验、风险与结构化研究目标",
    input_schema={
        "type": "object",
        "properties": {"idea_id": {"type": "string", "description": "想法 uuid"}},
        "required": ["idea_id"],
    },
    summarize=lambda a, r: f"想法详情：{r.get('title', a.get('idea_id', ''))}",
)
async def get_idea(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    try:
        idea_id = uuid.UUID(str(args.get("idea_id")))
    except ValueError as e:
        raise ValueError(f"idea_id 不是合法 uuid：{args.get('idea_id')}") from e
    async with get_sessionmaker()() as session:
        idea = await session.get(Idea, idea_id)
        if idea is None or idea.project_id not in await project_ids_for(session, ctx):
            raise ValueError(f"范围内不存在该想法：{args.get('idea_id')}")
        brief = _idea_brief(idea)
        brief["content"] = (idea.content or "")[:_CONTENT_CHARS] or None
        brief["goal"] = idea.goal
        brief["evidence"] = idea.evidence
        return brief


@tool(
    "list_experiments",
    description="列出本课题的实验，含状态与关联想法标题",
    input_schema={"type": "object", "properties": {}},
    summarize=lambda a, r: f"实验清单（{len(r.get('experiments') or [])} 条）",
)
async def list_experiments(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    async with get_sessionmaker()() as session:
        rows = await experiments_service.list_experiments(
            session, project_ids=await project_ids_for(session, ctx)
        )
    return {
        "experiments": [
            {
                "experiment_id": str(exp.id),
                "idea_title": idea_title,
                "status": exp.status,
            }
            for exp, idea_title in rows
        ]
    }


@tool(
    "get_experiment",
    description="取某个实验的详情，含假设、计划、各轮运行记录与指标",
    input_schema={
        "type": "object",
        "properties": {"experiment_id": {"type": "string", "description": "实验 uuid"}},
        "required": ["experiment_id"],
    },
    summarize=lambda a, r: f"实验详情：{r.get('status', a.get('experiment_id', ''))}",
)
async def get_experiment(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    try:
        exp_id = uuid.UUID(str(args.get("experiment_id")))
    except ValueError as e:
        raise ValueError(f"experiment_id 不是合法 uuid：{args.get('experiment_id')}") from e
    async with get_sessionmaker()() as session:
        stmt = (
            select(Experiment, Idea.title)
            .join(Idea, Idea.id == Experiment.idea_id)
            .where(
                Experiment.id == exp_id,
                Experiment.project_id.in_(await project_ids_for(session, ctx)),
            )
            .options(selectinload(Experiment.runs))
        )
        row = (await session.execute(stmt)).first()
        if row is None:
            raise ValueError(f"范围内不存在该实验：{args.get('experiment_id')}")
        exp, idea_title = row
        return experiments_service.to_read(exp, idea_title).model_dump(mode="json")


@tool(
    "read_experiment_logs",
    description="读取实验某一轮运行的训练日志尾部；未指定 run_id 时读取最近一轮",
    input_schema={
        "type": "object",
        "properties": {
            "experiment_id": {"type": "string", "description": "实验 uuid"},
            "run_id": {"type": "string", "description": "某一轮 uuid（见 get_experiment 的 runs）"},
            "tail": {"type": "integer", "minimum": 1, "maximum": _MAX_LOG_LINES, "default": 200},
        },
        "required": ["experiment_id"],
    },
    summarize=lambda a, r: f"实验日志（{len(r.get('lines') or [])} 行）",
)
async def read_experiment_logs(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    try:
        exp_id = uuid.UUID(str(args.get("experiment_id")))
    except (ValueError, TypeError) as e:
        raise ValueError(f"experiment_id 不是合法 uuid：{args.get('experiment_id')}") from e
    raw_run = str(args.get("run_id") or "").strip()
    tail = min(_MAX_LOG_LINES, max(1, int(args.get("tail") or 200)))

    async with get_sessionmaker()() as session:
        stmt = (
            select(Experiment)
            .where(
                Experiment.id == exp_id,
                Experiment.project_id.in_(await project_ids_for(session, ctx)),
            )
            .options(selectinload(Experiment.runs))
        )
        exp = (await session.execute(stmt)).scalar_one_or_none()
        if exp is None:
            raise ValueError(f"范围内不存在该实验：{args.get('experiment_id')}")
        if raw_run:
            try:
                run_id = uuid.UUID(raw_run)
            except ValueError as e:
                raise ValueError(f"run_id 不是合法 uuid：{raw_run}") from e
            run = next((r for r in exp.runs if r.id == run_id), None)
            if run is None:
                raise ValueError(f"实验里没有这一轮：{raw_run}")
        else:
            run = experiments_service.latest_run(exp)
        if run is None:
            return {
                "experiment_id": str(exp_id),
                "run_id": None,
                "lines": [],
                "note": "实验还没有运行记录",
            }
        log_path, run_seq, run_status = run.log_path, run.seq, run.status

    lines, truncated = experiments_service.read_local_log_tail(log_path, tail)
    return {
        "experiment_id": str(exp_id),
        "run_id": str(run.id),
        "seq": run_seq,
        "run_status": run_status,
        "truncated": truncated,
        "lines": lines,
    }


@tool(
    "get_fact_pack",
    description="取某份稿件的事实包，含关联想法、实验假设与指标、图表及可引用文献，供写作取材使用",
    input_schema={
        "type": "object",
        "properties": {"manuscript_id": {"type": "string", "description": "稿件 uuid"}},
        "required": ["manuscript_id"],
    },
    summarize=lambda a, r: "取事实包（fact_pack）",
)
async def get_fact_pack(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    try:
        ms_id = uuid.UUID(str(args.get("manuscript_id")))
    except ValueError as e:
        raise ValueError(f"manuscript_id 不是合法 uuid：{args.get('manuscript_id')}") from e
    async with get_sessionmaker()() as session:
        manuscript = await session.get(Manuscript, ms_id)
        if manuscript is None or manuscript.project_id not in await project_ids_for(session, ctx):
            raise ValueError(f"范围内不存在该稿件：{args.get('manuscript_id')}")
        return await manuscripts_service.build_fact_pack(session, manuscript)
