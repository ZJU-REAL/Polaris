"""Voyage — 长时程 agent 核心（docs/architecture.md §3）。

三元组闭环：Navigator（规划/重规划）· Helm（执行）· Sextant（自验证），
由 engine.py 的持久化状态机驱动，支持断点恢复、人在环闸门、协作式取消。
"""

from app.agents.voyage import (
    actions_experiment,  # noqa: F401  注册 experiment.* 动作
    actions_ideas,  # noqa: F401  注册 forge.* / review.* 动作
    actions_wiki,  # noqa: F401  注册 wiki.* 动作
    actions_writing,  # noqa: F401  注册 writing.* 动作
)
from app.agents.voyage.engine import VoyageEngine

__all__ = ["VoyageEngine"]
