"""ARQ 任务。

当前仅示例任务。未来任务归属（M2+）：
- survey_*: 文献抓取/解析/去重/水位线（确定性逻辑）+ LLM 打分编纂
- ideation_*: 想法生成与 Elo 锦标赛
- experiment_*: 远程实验 setup/run/监控（asyncssh；写操作先过 Gate）
- writing_*: 稿件生成与编译
"""

from typing import Any


async def ping_task(ctx: dict[str, Any], message: str = "ping") -> str:
    """连通性验证用示例任务。"""
    return f"pong: {message}"
