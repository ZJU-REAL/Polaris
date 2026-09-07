"""Runner v2 注册表：backend id → 插件工厂，`plan.backend` 显式分派。

替代 v1 的「plan 有无 container」二值推断：后端选择是**显式数据**（plan.backend），
默认 `python-ml` 保全兼容——今天所有存量实验不写 backend，走 python-ml，行为不变。
实验 kind（eval/training/…）退役为科学语义标签，不再暗示执行后端。

分派现状与迁移计划：本阶段（R1）注册表与 python-ml 适配器就位，`resolve_backend`
可拿到并驱动插件，但 **actions_experiment 仍走旧路**（直接调 open_runner + 19 原语）——
切换动作层到 v2 分派是 R4 接入第一个非 ML 后端（openfoam/ngspice）时的事：届时
动作层按 resolve_backend 拿插件，python-ml 的行为因适配器全部委托存量原语而保持逐字节一致。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from app.services.runners.contract import RunnerPlugin
from app.services.runners.python_ml import PYTHON_ML_BACKEND, PythonMLRunner

DEFAULT_BACKEND = PYTHON_ML_BACKEND  # 缺省后端：兼容全部存量实验


class UnknownBackendError(ValueError):
    """plan.backend 指了注册表里没有的后端（拼错/插件未安装）。"""

    def __init__(self, backend: str, known: list[str]) -> None:
        self.backend = backend
        self.known = known
        super().__init__(f"unknown runner backend {backend!r}; known: {', '.join(known)}")


# 后端 id → 插件工厂。工厂收关键字参数（如 python-ml 的 runner=执行底座），返回插件实例；
# 存工厂而非单例，是因为插件实例绑定一次 run 的底座（SSH 会话等），不能跨 run 复用。
_FACTORIES: dict[str, Callable[..., RunnerPlugin]] = {}


def register(backend: str, factory: Callable[..., RunnerPlugin]) -> None:
    """注册后端插件工厂。重名直接拒绝——静默覆盖会把分派变成 import 顺序问题。"""
    key = backend.strip()
    if not key:
        raise ValueError("runner backend id must be non-empty")
    if key in _FACTORIES:
        raise ValueError(f"runner backend already registered: {key!r}")
    _FACTORIES[key] = factory


def get(backend: str) -> Callable[..., RunnerPlugin]:
    """按 id 取插件工厂；未注册 → UnknownBackendError。"""
    factory = _FACTORIES.get(backend)
    if factory is None:
        raise UnknownBackendError(backend, known_backends())
    return factory


def known_backends() -> list[str]:
    """已注册后端 id（排序稳定；schema 校验 params.backend ∈ 此集合）。"""
    return sorted(_FACTORIES)


def resolve_backend(plan: Any, **substrate: Any) -> RunnerPlugin:
    """分派入口：读 plan.backend（缺省 python-ml），从注册表实例化插件。

    substrate 关键字参数透传给插件工厂（python-ml 需要 runner=现有执行底座）。
    plan.backend 是受校验数据（ExperimentParams 入口已限定 ∈ 注册表），这里再兜一道：
    未注册后端抛 UnknownBackendError，绝不静默回退——静默回退会把「插件没装」变成
    「用错后端跑了实验」。
    """
    backend = DEFAULT_BACKEND
    if isinstance(plan, dict):
        raw = str(plan.get("backend") or "").strip()
        if raw:
            backend = raw
    return get(backend)(**substrate)


# —— 内建后端注册（新后端在各自模块里定义工厂，在这里挂进注册表）——
register(PYTHON_ML_BACKEND, PythonMLRunner.create)
