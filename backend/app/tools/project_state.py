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

_CONTENT_CHARS = 4000


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
    description="项目想法清单（可按 status/depth/research_type 过滤）",
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
            project_id=ctx.project_id,
            status=str(args.get("status") or "") or None,
            depth=str(args.get("depth") or "") or None,
            research_type=str(args.get("research_type") or "") or None,
        )
    return {"ideas": [_idea_brief(i) for i in ideas[:50]]}


@tool(
    "get_idea",
    description="取某想法的完整内容（动机/方法/预期实验/风险 + 结构化 goal）",
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
        if idea is None or idea.project_id != ctx.project_id:
            raise ValueError(f"项目内不存在该想法：{args.get('idea_id')}")
        brief = _idea_brief(idea)
        brief["content"] = (idea.content or "")[:_CONTENT_CHARS] or None
        brief["goal"] = idea.goal
        brief["evidence"] = idea.evidence
        return brief


@tool(
    "list_experiments",
    description="项目实验清单（状态 + 关联想法标题）",
    input_schema={"type": "object", "properties": {}},
    summarize=lambda a, r: f"实验清单（{len(r.get('experiments') or [])} 条）",
)
async def list_experiments(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    async with get_sessionmaker()() as session:
        rows = await experiments_service.list_experiments(session, project_id=ctx.project_id)
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
    description="取某实验详情（假设/计划/运行记录与指标）",
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
            .where(Experiment.id == exp_id, Experiment.project_id == ctx.project_id)
            .options(selectinload(Experiment.runs))
        )
        row = (await session.execute(stmt)).first()
        if row is None:
            raise ValueError(f"项目内不存在该实验：{args.get('experiment_id')}")
        exp, idea_title = row
        return experiments_service.to_read(exp, idea_title).model_dump(mode="json")


@tool(
    "get_fact_pack",
    description="取稿件的事实包：关联想法 + 实验假设/指标/图表 + 项目引用（写作取料用）",
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
        if manuscript is None or manuscript.project_id != ctx.project_id:
            raise ValueError(f"项目内不存在该稿件：{args.get('manuscript_id')}")
        return await manuscripts_service.build_fact_pack(session, manuscript)
