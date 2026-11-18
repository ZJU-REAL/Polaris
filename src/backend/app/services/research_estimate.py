"""开始一次研究之前的估算：大约多少 token、多少钱、多少分钟（不 import fastapi）。

给界面在「开始」按钮旁边说一句「大约 $X、Y 分钟」。两个来源：

- **有历史**：取最近几次跑完的研究，按它们的实际用量（``llm_usage``，含缓存折扣）
  和各步耗时求平均。调研的开销分成「每筛一篇」和「每写一篇解读」两个单价，随这次
  要筛、要写的篇数伸缩；寻找假设与文献量关系不大，按每次的平均值算。
- **没有历史**：用内置的单价（真实跑出来的数，见 :data:`DEFAULTS`）。

给的是一个区间（估值的 0.7–1.5 倍），不是一个数：检索到多少篇、有多少篇拿得到全文、
模型想多久，每次都不一样。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.llm import pricing
from app.models.llm_config import LLMUsage
from app.models.research_run import ResearchRun
from app.models.voyage import VoyageRun, VoyageStep

#: 取最近几次跑完的研究求平均。
HISTORY_RUNS = 5
#: 区间的下、上界相对估值的倍数。
RANGE_LOW, RANGE_HIGH = 0.7, 1.5

#: 筛选阶段的步骤（开销随「筛多少篇」变）；调研里其余步骤随「写多少篇解读」变。
_SCREEN_ACTIONS = frozenset(
    {"wiki.search_candidates", "wiki.score_relevance", "wiki.expand_citations", "wiki.snowball"}
)
_SCREEN_STAGES = frozenset({"relevance", "survey_queries"})


@dataclass(frozen=True, slots=True)
class UnitCosts:
    """估算用的单价。token 是模型调用的输入 + 输出（不含向量化）。"""

    screen_tokens: float  # 每筛一篇
    screen_usd: float
    screen_seconds: float
    summary_tokens: float  # 每写一篇解读（含取全文、建索引）
    summary_usd: float
    summary_seconds: float
    hypotheses_tokens: float  # 寻找假设（一次）
    hypotheses_usd: float
    hypotheses_seconds: float


#: 没有历史时用的单价：claude-opus-5、60 篇筛选 / 20 篇解读的真实运行量出来的。
DEFAULTS = UnitCosts(
    screen_tokens=3_800,
    screen_usd=0.025,
    screen_seconds=4.5,
    summary_tokens=14_000,
    summary_usd=0.11,
    summary_seconds=22.0,
    hypotheses_tokens=700_000,
    hypotheses_usd=3.5,
    hypotheses_seconds=540.0,
)


async def _stage_usage(session: AsyncSession, voyage_id: uuid.UUID) -> list[dict[str, Any]]:
    rows = (
        await session.execute(
            select(
                LLMUsage.stage,
                LLMUsage.model,
                func.sum(LLMUsage.prompt_tokens),
                func.sum(LLMUsage.completion_tokens),
                func.sum(LLMUsage.cache_read_tokens),
                func.sum(LLMUsage.cache_write_tokens),
            )
            .where(LLMUsage.voyage_id == voyage_id)
            .group_by(LLMUsage.stage, LLMUsage.model)
        )
    ).all()
    out = []
    for stage, model, prompt, completion, cache_read, cache_write in rows:
        if pricing.price_for(model, stage) is None:
            continue  # 向量 / 重排：不计价，也不算进「模型用了多少 token」
        out.append(
            {
                "stage": stage,
                "tokens": int(prompt or 0) + int(completion or 0),
                "usd": pricing.cost_usd(
                    model=model,
                    stage=stage,
                    prompt_tokens=int(prompt or 0),
                    completion_tokens=int(completion or 0),
                    cache_read_tokens=int(cache_read or 0),
                    cache_write_tokens=int(cache_write or 0),
                ),
            }
        )
    return out


async def _step_seconds(session: AsyncSession, voyage_id: uuid.UUID) -> list[tuple[str, float]]:
    rows = (
        await session.execute(
            select(VoyageStep.action, VoyageStep.started_at, VoyageStep.finished_at).where(
                VoyageStep.run_id == voyage_id
            )
        )
    ).all()
    return [
        (action, (finished - started).total_seconds())
        for action, started, finished in rows
        if started is not None and finished is not None
    ]


def _voyage_id(run: ResearchRun, phase: str) -> uuid.UUID | None:
    state = (run.phases or {}).get(phase) if isinstance(run.phases, dict) else None
    try:
        return uuid.UUID(str((state or {}).get("voyage_id")))
    except (TypeError, ValueError):
        return None


async def learned_unit_costs(session: AsyncSession, user_id: uuid.UUID) -> tuple[UnitCosts, int]:
    """从最近跑完的研究里学单价；返回 (单价, 用了几次研究)。一次都没有时返回默认值。"""
    runs = (
        (
            await session.execute(
                select(ResearchRun)
                .where(ResearchRun.created_by == user_id, ResearchRun.phase == "choose")
                .order_by(ResearchRun.created_at.desc())
                .limit(HISTORY_RUNS)
            )
        )
        .scalars()
        .all()
    )
    samples: list[UnitCosts] = []
    for run in runs:
        survey = await session.get(VoyageRun, _voyage_id(run, "survey") or uuid.UUID(int=0))
        hyp = await session.get(VoyageRun, _voyage_id(run, "hypotheses") or uuid.UUID(int=0))
        if survey is None or hyp is None or survey.status != "done" or hyp.status != "done":
            continue
        checkpoint = survey.checkpoint if isinstance(survey.checkpoint, dict) else {}
        stats = checkpoint.get("ingest_search_stats")
        screened = int((stats or {}).get("inserted") or 0) if isinstance(stats, dict) else 0
        summarised = int(checkpoint.get("compiled_count") or 0)
        if screened <= 0 or summarised <= 0:
            continue
        usage = await _stage_usage(session, survey.id)
        if not usage:
            continue  # 记账出现之前的任务：没有可学的数
        screen = [u for u in usage if u["stage"] in _SCREEN_STAGES]
        rest = [u for u in usage if u["stage"] not in _SCREEN_STAGES]
        steps = await _step_seconds(session, survey.id)
        hyp_usage = await _stage_usage(session, hyp.id)
        samples.append(
            UnitCosts(
                screen_tokens=sum(u["tokens"] for u in screen) / screened,
                screen_usd=sum(u["usd"] for u in screen) / screened,
                screen_seconds=sum(s for a, s in steps if a in _SCREEN_ACTIONS) / screened,
                summary_tokens=sum(u["tokens"] for u in rest) / summarised,
                summary_usd=sum(u["usd"] for u in rest) / summarised,
                summary_seconds=sum(s for a, s in steps if a not in _SCREEN_ACTIONS) / summarised,
                hypotheses_tokens=sum(u["tokens"] for u in hyp_usage),
                hypotheses_usd=sum(u["usd"] for u in hyp_usage),
                hypotheses_seconds=sum(s for _a, s in await _step_seconds(session, hyp.id)),
            )
        )
    if not samples:
        return DEFAULTS, 0
    fields = UnitCosts.__dataclass_fields__
    mean = {name: sum(getattr(s, name) for s in samples) / len(samples) for name in fields}
    return UnitCosts(**mean), len(samples)


def estimate_from(units: UnitCosts, *, max_papers: int, compile_top_n: int) -> dict[str, Any]:
    """单价 × 篇数 → 区间（纯函数）。解读篇数不会超过筛选篇数。"""
    top_n = min(compile_top_n, max_papers)
    tokens = (
        units.screen_tokens * max_papers + units.summary_tokens * top_n + units.hypotheses_tokens
    )
    usd = units.screen_usd * max_papers + units.summary_usd * top_n + units.hypotheses_usd
    seconds = (
        units.screen_seconds * max_papers + units.summary_seconds * top_n + units.hypotheses_seconds
    )
    return {
        "tokens_low": int(tokens * RANGE_LOW),
        "tokens_high": int(tokens * RANGE_HIGH),
        "cost_usd_low": round(usd * RANGE_LOW, 2),
        "cost_usd_high": round(usd * RANGE_HIGH, 2),
        "minutes_low": max(1, round(seconds * RANGE_LOW / 60)),
        "minutes_high": max(1, round(seconds * RANGE_HIGH / 60)),
    }
