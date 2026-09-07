"""Runner v2：可插拔实验执行后端（契约 + 注册表 + 适配器）。

设计报告 §13（docs/rfcs/2026-09-02-polaris-2.0-design-report.zh.md）。
现有 ML runner（app/agents/voyage/runner.py 的 19 原语）作为第一个后端
`python-ml` 包在契约后面，行为零变化；新后端（openfoam/ngspice/fmu…）
按同一契约挂进注册表。
"""

from app.services.runners.contract import (
    Issue,
    LicenseNeed,
    MaterialSpec,
    ResourceNeeds,
    ResultBundle,
    ResultFile,
    RunContext,
    RunHandle,
    RunnerError,
    RunnerManifest,
    RunnerPlugin,
    RunStatus,
)
from app.services.runners.registry import (
    DEFAULT_BACKEND,
    UnknownBackendError,
    known_backends,
    manifest_for,
    resolve_backend,
)

__all__ = [
    "DEFAULT_BACKEND",
    "Issue",
    "LicenseNeed",
    "MaterialSpec",
    "ResourceNeeds",
    "ResultBundle",
    "ResultFile",
    "RunContext",
    "RunHandle",
    "RunStatus",
    "RunnerError",
    "RunnerManifest",
    "RunnerPlugin",
    "UnknownBackendError",
    "known_backends",
    "manifest_for",
    "resolve_backend",
]
