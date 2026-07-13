"""ARQ 任务。

M1：Voyage 引擎驱动任务（run/resume）。未来任务归属（M2+）：
- survey_*: 文献抓取/解析/去重/水位线（确定性逻辑）+ LLM 打分编纂
- ideation_*: 想法生成与 Elo 锦标赛
- experiment_*: 远程实验 setup/run/监控（asyncssh；写操作先过 Gate）
- writing_*: 稿件生成与编译
"""

import uuid
from typing import Any

from app.agents.voyage import VoyageEngine
from app.core.events import EventBus
from app.core.redis import get_redis


async def ping_task(ctx: dict[str, Any], message: str = "ping") -> str:
    """连通性验证用示例任务。"""
    return f"pong: {message}"


def _make_engine() -> VoyageEngine:
    return VoyageEngine(event_bus=EventBus(get_redis()))


async def run_voyage(ctx: dict[str, Any], run_id: str) -> None:
    """驱动一次新航程（POST /voyages 入队）。"""
    await _make_engine().run(uuid.UUID(run_id))


async def resume_voyage(ctx: dict[str, Any], run_id: str) -> None:
    """闸门批准后从断点恢复航程（gates approve 入队）。"""
    await _make_engine().resume(uuid.UUID(run_id))
