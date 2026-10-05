"""Experiment Lab 业务逻辑（不 import fastapi，docs/task-system.md §7（原 api-m4.md §2/§3））。

- 创建实验：校验 idea promoted + 凭据属本人 → Experiment 与 kind=experiment 的 voyage 1:1；
- 本地日志镜像：{data_dir}/experiments/<exp_id>/run_<seq>.log（logs API / SSE 只读本地镜像）；
- 取消：协作式 cancel voyage + 尽力 SSH kill 运行中的进程。
"""

import logging
import shutil
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import get_settings
from app.models.activity import Activity
from app.models.base import utcnow
from app.models.experiment import (
    EXPERIMENT_STATUSES,
    EXPERIMENT_TERMINAL_STATUSES,
    Experiment,
    ExperimentRun,
)
from app.models.idea import Idea
from app.models.project import Project
from app.models.resource import Resource
from app.models.ssh_credential import SSHCredential
from app.models.voyage import TERMINAL_STATUSES, VoyageRun
from app.schemas.experiment import (
    ExperimentCreate,
    ExperimentFigure,
    ExperimentRead,
    ExperimentRunRead,
)
from app.services import ssh_exec
from app.services.managed_commands import OperationContext, RepairScope, redact_text
from app.services.managed_ssh import ManagedCommandHandle, ManagedGPUUsage, ManagedStopResult
from app.services.projects import in_my_projects

logger = logging.getLogger("polaris.experiments")

# max_hours=0 = 无限时（用户定调：修复/运行只受显式时间预算约束，默认不设限）
DEFAULT_BUDGET: dict[str, Any] = {"max_hours": 0, "max_runs": 10, "no_improve_stop": 2}


class IdeaNotFoundError(Exception):
    """idea 不存在或不属于该项目。"""


class IdeaNotPromotedError(Exception):
    """idea 未晋级（status != promoted）。"""


class CredentialNotFoundError(Exception):
    """SSH 凭据不存在或不属于当前用户。"""


class RunnerHostNotFoundError(Exception):
    """指定的 runner 主机资源不存在/不属于当前用户/不是 host 类（#685）。"""


class RunnerHostUnavailableError(Exception):
    """runner 主机不可用：凭据已吊销（config.unavailable）或未关联凭据（#685）。"""


class ExperimentAlreadyFinishedError(Exception):
    """对终态实验执行 cancel。"""


# ---- 本地日志镜像 ----


def local_log_path(experiment_id: uuid.UUID | str, seq: int) -> Path:
    return Path(get_settings().data_dir) / "experiments" / str(experiment_id) / f"run_{seq}.log"


def append_local_log(experiment_id: uuid.UUID | str, seq: int, text: str) -> Path:
    """Append a redacted copy of run output to the user-visible log mirror.

    Metric parsing happens before this function is called, so keeping secrets
    off disk does not alter the experiment's structured results.
    """
    path = local_log_path(experiment_id, seq)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(redact_text(text))
    return path


def terminal_log_path(experiment_id: uuid.UUID | str) -> Path:
    """Local mirror for raw stdout/stderr from every managed remote command."""
    return Path(get_settings().data_dir) / "experiments" / str(experiment_id) / "terminal.log"


def append_terminal_output(
    experiment_id: uuid.UUID | str,
    *,
    operation: str,
    stream: str,
    text: str,
) -> Path:
    """远端命令原始输出落盘。

    写之前先脱敏：这个文件通过 /experiments/{id}/terminal-logs 原样发给课题主人
    与平台管理员，是这套机制里最大的一个外露面。快照和失败报告都脱敏了，这里不脱
    就等于白做——实验脚本里 echo 一次 HF_TOKEN 就直接进了所有人的终端面板。
    """
    path = terminal_log_path(experiment_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    safe_text = redact_text(text)
    prefix = f"[{operation}][{stream}] "
    with path.open("a", encoding="utf-8") as file:
        for line in safe_text.splitlines(keepends=True):
            file.write(prefix + line)
        if safe_text and not safe_text.endswith(("\n", "\r")):
            file.write("\n")
    return path


def read_local_log_tail(path_str: str | None, tail: int) -> tuple[list[str], bool]:
    """读本地镜像的最后 tail 行；文件缺失返回空。"""
    if not path_str:
        return [], False
    path = Path(path_str)
    if not path.is_file():
        return [], False
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    if tail <= 0 or len(lines) <= tail:
        return lines, False
    return lines[-tail:], True


# ---- 本地图表镜像（docs/task-system.md §7（原 api-m5-a.md §1） figures 步骤拉回落盘） ----


def figures_dir(experiment_id: uuid.UUID | str) -> Path:
    return Path(get_settings().data_dir) / "experiments" / str(experiment_id) / "figures"


def figure_local_path(experiment_id: uuid.UUID | str, name: str) -> Path:
    return figures_dir(experiment_id) / name


# ---- 创建 ----


async def create_experiment(
    session: AsyncSession,
    *,
    project: Project,
    data: ExperimentCreate,
    user_id: uuid.UUID,
) -> tuple[Experiment, VoyageRun, str]:
    idea = await session.get(Idea, data.idea_id)
    if idea is None or idea.project_id != project.id:
        raise IdeaNotFoundError(str(data.idea_id))
    if idea.status != "promoted":
        raise IdeaNotPromotedError(str(idea.id))
    # 执行目标二选一（#685）：resource_id 指定已注册的 runner 主机（凭据从资源上取，
    # 顺带吃到租约/吊销联动那套语义）；不给则按老路径直接用 credential_id——
    # 存量前端只传 credential_id，行为零变化。
    credential_id = data.credential_id
    if data.resource_id is not None:
        resource = await session.get(Resource, data.resource_id)
        if resource is None or resource.owner_id != user_id or resource.kind != "host":
            raise RunnerHostNotFoundError(str(data.resource_id))
        if (resource.config or {}).get("unavailable") or resource.credential_id is None:
            raise RunnerHostUnavailableError(str(data.resource_id))
        credential_id = resource.credential_id
    # 凭据放宽（#716）：是否强制 SSH 凭据由后端 manifest 自述（credential_kinds）。
    # python-ml 声明 "ssh" → 维持原状（必须给凭据，检查逐字节同前）；本机底座后端
    # （ngspice/openfoam/fmu，credential_kinds 为空）不吃连接凭据 → credential_id
    # 可空。给了就照旧校验归属/kind——错的凭据不因后端用不上而放行。
    # 延迟导入：runners 注册表反向经 python-ml 适配器牵到 voyage 动作层，顶层互 import
    # 会让 fmu_worker 这类以 runners 为入口的进程死在环上（实测炸过 fmu 子进程测试）
    from app.services.runners.registry import manifest_for

    backend = data.params.backend if data.params else "python-ml"
    needs_ssh = "ssh" in manifest_for(backend).credential_kinds
    credential = None
    if credential_id is not None:
        credential = await session.get(SSHCredential, credential_id)
        # kind 校验（#677 凭据多态化后）：实验直跑只吃 ssh 凭据，别的 kind 私钥列为空，
        # 放进来会在 ssh_exec 解密时炸得不知所云——这里当不存在处理
        if credential is None or credential.user_id != user_id or credential.kind != "ssh":
            raise CredentialNotFoundError(str(credential_id))
    elif needs_ssh:
        # schema 的条件校验已挡住这条路；服务层兜底（不走 schema 的直接调用方）
        raise CredentialNotFoundError(str(credential_id))

    params = data.params
    budget = dict(DEFAULT_BUDGET)
    if params and params.budget:
        budget |= params.budget.model_dump()

    experiment = Experiment(
        project_id=project.id,
        idea_id=idea.id,
        credential_id=credential.id if credential is not None else None,
        status="planning",
        budget=budget,
        server_host=credential.host if credential is not None else None,
    )
    session.add(experiment)
    await session.flush()
    experiment.workdir = ssh_exec.workdir_for(str(experiment.id))

    voyage = VoyageRun(
        kind="experiment",
        goal=f"实验验证：{idea.title}",
        status="planning",
        cursor=0,
        checkpoint={
            "params": {
                "experiment_id": str(experiment.id),
                "gpu_hint": params.gpu_hint if params else None,
                # 预算闸门开关（默认 False=不拦）：navigator 与 experiment.plan 都读这里
                "confirm_budget": bool(params.confirm_budget) if params else False,
                "eval_model": params.eval_model if params else None,
                "hf_mirror": bool(params.hf_mirror) if params else False,
                "extra_notes": params.extra_notes if params else None,
                # 开题问答（AI 按 idea 生成的问题 + 用户回答）：进 plan/codegen prompt
                "intake": (
                    [qa.model_dump() for qa in params.intake] if params and params.intake else None
                ),
                # 执行后端（Runner v2，#675/#716）：动作层分派点
                # （actions_experiment._resolve_runner_plugin）从这里读，经注册表拿插件
                "backend": params.backend if params else "python-ml",
                # 流程包（#678）：非空时 navigator 按包生成计划；None = 原路径
                "process_pack": params.process_pack if params else None,
                # runner 主机资源（#685/#716）：非空 = 用户指定跑在哪台注册机器上；
                # experiment_setup 备环境前据此排队获取租约（resource_leases）
                "resource_id": str(data.resource_id) if data.resource_id else None,
            }
        },
        budget=None,
        project_id=project.id,
        created_by=user_id,
    )
    session.add(voyage)
    await session.flush()
    experiment.voyage_id = voyage.id

    session.add(
        Activity(
            project_id=project.id,
            actor=f"user:{user_id}",
            kind="experiment.created",
            message=f"实验已创建：{idea.title}",
            payload={
                "experiment_id": str(experiment.id),
                "idea_id": str(idea.id),
                "voyage_id": str(voyage.id),
                "budget": budget,
            },
        )
    )
    await session.commit()
    await session.refresh(experiment)
    await session.refresh(voyage)
    return experiment, voyage, idea.title


# ---- 读取 ----


def to_read(experiment: Experiment, idea_title: str) -> ExperimentRead:
    return ExperimentRead(
        id=experiment.id,
        project_id=experiment.project_id,
        idea_id=experiment.idea_id,
        idea_title=idea_title,
        status=experiment.status,
        voyage_id=experiment.voyage_id,
        workdir=experiment.workdir,
        server_host=experiment.server_host,
        budget=experiment.budget,
        trashed_at=experiment.trashed_at,
        created_at=experiment.created_at,
        updated_at=experiment.updated_at,
    )


def serialize_runs(experiment: Experiment) -> list[ExperimentRunRead]:
    return [ExperimentRunRead.model_validate(r) for r in experiment.runs]


def serialize_figures(experiment: Experiment) -> list[ExperimentFigure]:
    """图表列表出 API：只暴露 index/name/caption（内部 path 不出去）。"""
    return [
        ExperimentFigure(
            index=int(f["index"]), name=str(f["name"]), caption=f.get("caption") or None
        )
        for f in experiment.figures or []
    ]


async def list_experiments(
    session: AsyncSession, *, project_ids: Sequence[uuid.UUID], trashed: bool = False
) -> list[tuple[Experiment, str]]:
    """这些课题下的实验。

    收一个列表而不是单个 id：界面上问的是「这个课题的实验」（传一个），而助手在
    不收窄课题时问的是「我能看到的实验」（传全部参与的课题）。以前只能传一个，
    助手那条路就只好传 None，SQL 里成了 project_id = NULL，一条都匹配不到。
    """
    if not project_ids:
        return []
    trash_cond = Experiment.trashed_at.is_not(None) if trashed else Experiment.trashed_at.is_(None)
    order = Experiment.trashed_at.desc() if trashed else Experiment.created_at.desc()
    stmt = (
        select(Experiment, Idea.title)
        .join(Idea, Idea.id == Experiment.idea_id)
        .where(Experiment.project_id.in_(project_ids), trash_cond)
        .order_by(order)
    )
    return [(exp, title) for exp, title in (await session.execute(stmt)).all()]


async def _owned_experiments(
    session: AsyncSession, *, project_id: uuid.UUID, ids: list[uuid.UUID]
) -> list[Experiment]:
    if not ids:
        return []
    stmt = select(Experiment).where(Experiment.project_id == project_id, Experiment.id.in_(ids))
    return list((await session.execute(stmt)).scalars().all())


async def trash_experiments(
    session: AsyncSession, *, project_id: uuid.UUID, ids: list[uuid.UUID]
) -> int:
    now = datetime.now(UTC)
    n = 0
    for exp in await _owned_experiments(session, project_id=project_id, ids=ids):
        if exp.trashed_at is None:
            if exp.status not in EXPERIMENT_TERMINAL_STATUSES:
                # 运行中的实验先取消（协作式停 voyage + 尽力 kill 远端进程）。
                # 否则 voyage 成为孤儿：拿着已删实验的 id 反复失败重规划——线上实测
                # purge 后 voyage 在「experiment not found」上烧了两轮计划调整才被人叫停。
                try:
                    await cancel_experiment(session, exp)
                except Exception:  # noqa: BLE001 — 取消失败不能挡住回收本身
                    logger.exception("cancel before trash failed: %s", exp.id)
            exp.trashed_at = now
            n += 1
    await session.commit()
    return n


async def restore_experiments(
    session: AsyncSession, *, project_id: uuid.UUID, ids: list[uuid.UUID]
) -> int:
    n = 0
    for exp in await _owned_experiments(session, project_id=project_id, ids=ids):
        if exp.trashed_at is not None:
            exp.trashed_at = None
            n += 1
    await session.commit()
    return n


async def purge_experiments(
    session: AsyncSession, *, project_id: uuid.UUID, ids: list[uuid.UUID] | None = None
) -> int:
    """永久删除。ids=None → 清空该项目回收站。删本地目录（日志/图）；runs 走 DB 级联。
    远端 workdir 不动（best-effort，避免误删共享服务器）。返回删除数量。"""
    if ids is None:
        rows = [
            exp
            for exp, _ in await list_experiments(session, project_ids=[project_id], trashed=True)
        ]
    else:
        rows = [
            e
            for e in await _owned_experiments(session, project_id=project_id, ids=ids)
            if e.trashed_at is not None
        ]
    n = len(rows)
    for exp in rows:
        exp_dir = Path(get_settings().data_dir) / "experiments" / str(exp.id)
        shutil.rmtree(exp_dir, ignore_errors=True)
        await session.delete(exp)
    await session.commit()
    return n


async def get_experiment_for_user(
    session: AsyncSession, *, experiment_id: uuid.UUID, user_id: uuid.UUID
) -> tuple[Experiment, str] | None:
    """取实验（含 runs）；非课题主人视为不存在（不泄露存在性）。"""
    stmt = (
        select(Experiment, Idea.title)
        .join(Idea, Idea.id == Experiment.idea_id)
        .where(
            Experiment.id == experiment_id,
            in_my_projects(Experiment.project_id, user_id),
        )
        .options(selectinload(Experiment.runs))
    )
    row = (await session.execute(stmt)).first()
    return (row[0], row[1]) if row else None


def latest_run(experiment: Experiment) -> ExperimentRun | None:
    return max(experiment.runs, key=lambda r: r.seq, default=None)


# ---- 状态联动 ----


async def fail_by_voyage(session: AsyncSession, voyage_id: uuid.UUID) -> Experiment | None:
    """闸门驳回 / 用户拍板放弃等场景：关联实验（非终态）置 failed，返回该实验。"""
    stmt = select(Experiment).where(Experiment.voyage_id == voyage_id)
    experiment = (await session.execute(stmt)).scalar_one_or_none()
    if experiment is None or experiment.status in EXPERIMENT_TERMINAL_STATUSES:
        return None
    experiment.status = "failed"
    await session.commit()
    await session.refresh(experiment)
    return experiment


async def complete_by_voyage(session: AsyncSession, voyage_id: uuid.UUID) -> Experiment | None:
    """voyage 到达 done：仍非终态的关联实验落定为 done。

    覆盖两条路：完成标准通过（正常收尾）与用户拍板「接受当前结果为完成」。
    报告动作只在最后一轮成功时自己写 done（failed 只由人拍板，见 #366）——
    其余情况实验状态一路保持非终态，最终在这里随 voyage 落定。"""
    stmt = select(Experiment).where(Experiment.voyage_id == voyage_id)
    experiment = (await session.execute(stmt)).scalar_one_or_none()
    if experiment is None or experiment.status in EXPERIMENT_TERMINAL_STATUSES:
        return None
    experiment.status = "done"
    await session.commit()
    await session.refresh(experiment)
    return experiment


async def mark_waiting_by_voyage(session: AsyncSession, voyage_id: uuid.UUID) -> Experiment | None:
    """voyage 转 paused_ask：实验镜像 waiting_user（原状态记进 iteration_state，
    回答后由 :func:`resume_from_waiting_by_voyage` 恢复）。"""
    stmt = select(Experiment).where(Experiment.voyage_id == voyage_id)
    experiment = (await session.execute(stmt)).scalar_one_or_none()
    if (
        experiment is None
        or experiment.status in EXPERIMENT_TERMINAL_STATUSES
        or experiment.status == "waiting_user"
    ):
        return None
    state = dict(experiment.iteration_state or {})
    state["status_before_ask"] = experiment.status
    experiment.iteration_state = state
    experiment.status = "waiting_user"
    await session.commit()
    await session.refresh(experiment)
    return experiment


async def resume_from_waiting_by_voyage(
    session: AsyncSession, voyage_id: uuid.UUID
) -> Experiment | None:
    """回答提问后：waiting_user 恢复为提问前的状态（后续动作会自行推进状态）。"""
    stmt = select(Experiment).where(Experiment.voyage_id == voyage_id)
    experiment = (await session.execute(stmt)).scalar_one_or_none()
    if experiment is None or experiment.status != "waiting_user":
        return None
    state = dict(experiment.iteration_state or {})
    previous = state.pop("status_before_ask", None)
    experiment.iteration_state = state
    if previous not in EXPERIMENT_STATUSES or previous in EXPERIMENT_TERMINAL_STATUSES:
        previous = "running"
    experiment.status = str(previous)
    await session.commit()
    await session.refresh(experiment)
    return experiment


async def _open_cleanup_executor(experiment: Experiment, credential: SSHCredential) -> Any:
    from app.agents.voyage.runner import ContainerRunner, parse_container_spec

    executor = await ssh_exec.open_executor(
        credential=credential, exp_id=str(experiment.id), project_id=experiment.project_id,
    )
    spec = parse_container_spec((experiment.plan or {}).get("container"))
    return ContainerRunner.from_executor(executor, spec=spec) if spec is not None else executor


async def stop_managed_command_by_voyage(
    session: AsyncSession,
    voyage_id: uuid.UUID,
    handle_data: dict[str, Any],
) -> ManagedStopResult:
    """Stop exactly the attempt shown in a managed-command ask and verify it died."""
    experiment = (
        await session.execute(select(Experiment).where(Experiment.voyage_id == voyage_id))
    ).scalar_one_or_none()
    if experiment is None or experiment.credential_id is None:
        return ManagedStopResult(status="experiment_unavailable", confirmed=False)
    credential = await session.get(SSHCredential, experiment.credential_id)
    handle = managed_handle_from_data(handle_data)
    if credential is None or handle is None:
        return ManagedStopResult(status="invalid_handle", confirmed=False)
    executor = await _open_cleanup_executor(experiment, credential)
    try:
        await _bind_managed_workspace(session, executor, experiment, handle)
        return await executor.stop_managed_command(handle)
    finally:
        await executor.close()


def managed_handle_from_data(handle_data: Any) -> ManagedCommandHandle | None:
    """Restore a validated handle saved in a managed-command ask payload."""
    if not isinstance(handle_data, dict):
        return None
    context_data = handle_data.get("context")
    if not isinstance(context_data, dict):
        return None
    try:
        context = OperationContext(
            phase=str(context_data["phase"]),
            operation=str(context_data["operation"]),
            display_command=str(context_data["display_command"]),
            target=str(context_data["target"]) if context_data.get("target") else None,
            soft_timeout_seconds=context_data.get("soft_timeout_seconds"),
            stall_timeout_seconds=context_data.get("stall_timeout_seconds"),
            hard_timeout_seconds=context_data.get("hard_timeout_seconds"),
            repair_scope=RepairScope(str(context_data.get("repair_scope") or "none")),
        )
        process_id = int(handle_data["process_id"])
        process_group_id = int(handle_data["process_group_id"])
        if process_id <= 0 or process_group_id <= 0:
            return None
        return ManagedCommandHandle(
            operation_id=str(handle_data["operation_id"]),
            attempt_id=str(uuid.UUID(str(handle_data["attempt_id"]))),
            context=context,
            process_id=process_id,
            process_group_id=process_group_id,
        )
    except (KeyError, TypeError, ValueError):
        return None


def _positive_pids(values: Any) -> set[int]:
    result: set[int] = set()
    for value in values if isinstance(values, (list, tuple, set)) else ():
        try:
            pid = int(value)
        except (ValueError, TypeError, OverflowError):
            continue
        if pid > 0:
            result.add(pid)
    return result


def _cleanup_identity(cleanup: dict[str, Any]) -> tuple[Any, ...]:
    handle = managed_handle_from_data(cleanup.get("handle"))
    return (
        cleanup.get("workspace_id"),
        (handle.operation_id, handle.attempt_id, handle.process_id, handle.process_group_id)
        if handle else cleanup.get("handle"),
        tuple(sorted(_positive_pids(cleanup.get("pids")))),
        cleanup.get("launch_attempt_id"),
        cleanup.get("operation_context"),
    )


def _run_workspace(experiment: Experiment, run: ExperimentRun) -> str | None:
    state = experiment.iteration_state or {}
    metadata = state.get("run_metadata") or {}
    return (metadata.get(str(run.id)) or {}).get("workspace_id")


async def _bind_managed_workspace(
    session: AsyncSession,
    executor: Any,
    experiment: Experiment,
    handle: ManagedCommandHandle,
) -> str | None:
    """Restore the workspace which owns this handle before reading remote files."""
    cleanup = (experiment.iteration_state or {}).get("remote_cleanup") or {}
    saved = managed_handle_from_data(cleanup.get("handle"))
    workspace_id = None
    if saved is not None and (
        saved.operation_id, saved.attempt_id, saved.process_id, saved.process_group_id
    ) == (handle.operation_id, handle.attempt_id, handle.process_id, handle.process_group_id):
        workspace_id = cleanup.get("workspace_id")
    if workspace_id is None:
        rows = (
            await session.execute(
                select(ExperimentRun).where(
                    ExperimentRun.experiment_id == experiment.id,
                    ExperimentRun.pid == handle.process_id,
                )
            )
        ).scalars().all()
        workspaces = {_run_workspace(experiment, row) for row in rows}
        workspaces.discard(None)
        if len(workspaces) > 1:
            raise ValueError("managed handle has ambiguous run workspace")
        workspace_id = next(iter(workspaces), None)
    if workspace_id is not None:
        executor.bind_run_workspace(workspace_id)
    return workspace_id


async def managed_command_gpu_usage_by_voyage(
    session: AsyncSession,
    voyage_id: uuid.UUID,
    handle_data: dict[str, Any],
) -> ManagedGPUUsage:
    """Inspect GPU use for exactly the attempt referenced by an open ask."""
    experiment = (
        await session.execute(select(Experiment).where(Experiment.voyage_id == voyage_id))
    ).scalar_one_or_none()
    handle = managed_handle_from_data(handle_data)
    if experiment is None or experiment.credential_id is None or handle is None:
        return ManagedGPUUsage(status="invalid_handle", process_alive=False)
    credential = await session.get(SSHCredential, experiment.credential_id)
    if credential is None:
        return ManagedGPUUsage(status="credential_unavailable", process_alive=False)
    executor = await _open_cleanup_executor(experiment, credential)
    try:
        await _bind_managed_workspace(session, executor, experiment, handle)
        return await executor.managed_command_gpu_usage(handle)
    finally:
        await executor.close()


# ---- 取消 ----


async def cancel_experiment(session: AsyncSession, experiment: Experiment) -> Experiment:
    """Persist cancellation, then verify remote cleanup before releasing capacity.

    SSH failure does not block local cancellation. Its durable pending marker and
    running rows remain available to startup/periodic reconciliation.
    """
    if experiment.status in EXPERIMENT_TERMINAL_STATUSES:
        raise ExperimentAlreadyFinishedError(str(experiment.id))

    voyage = (
        await session.get(VoyageRun, experiment.voyage_id)
        if experiment.voyage_id is not None
        else None
    )
    if voyage is not None and voyage.status not in TERMINAL_STATUSES:
        voyage.status = "cancelled"

    runs = (
        await session.execute(
            select(ExperimentRun).where(
                ExperimentRun.experiment_id == experiment.id,
                ExperimentRun.status == "running",
            )
        )
    ).scalars().all()
    state = dict(experiment.iteration_state or {})
    cleanup = dict(state.get("remote_cleanup") or {})
    legacy_handle = (voyage.checkpoint or {}).get("managed_command_waiting") if voyage else None
    if legacy_handle and not cleanup.get("handle"):
        cleanup["handle"] = legacy_handle
    if runs or cleanup:
        cleanup.update(status="pending", reason="cancelled")
        cleanup["pids"] = sorted(
            _positive_pids(cleanup.get("pids"))
            | {int(run.pid) for run in runs if run.pid}
        )
        state["remote_cleanup"] = cleanup
        experiment.iteration_state = state

    experiment.status = "cancelled"
    session.add(
        Activity(
            project_id=experiment.project_id,
            actor="user",
            kind="experiment.cancelled",
            message="实验已取消",
            payload={"experiment_id": str(experiment.id)},
        )
    )
    await session.commit()
    await session.refresh(experiment)
    await reconcile_remote_cleanup(session, experiment)
    return experiment


async def reconcile_remote_cleanup(session: AsyncSession, experiment: Experiment) -> bool:
    """Retry terminal cleanup; release capacity only after external exit is confirmed.

    Managed handles are authoritative and belong to an immutable run workspace.
    A corrupt/replaced handle cannot fall back to a blind PID kill. Legacy root
    runs retain their observed-dead-PID fallback until their final reconciliation.
    """
    if experiment.status not in EXPERIMENT_TERMINAL_STATUSES:
        return False
    state = dict(experiment.iteration_state or {})
    raw_cleanup = state.get("remote_cleanup")
    if raw_cleanup is not None and not isinstance(raw_cleanup, dict):
        return False  # Corrupt durable ownership is not evidence that remote work exited.
    cleanup = dict(raw_cleanup or {})
    voyage = (
        await session.get(VoyageRun, experiment.voyage_id)
        if experiment.voyage_id is not None else None
    )
    legacy_handle = (voyage.checkpoint or {}).get("managed_command_waiting") if voyage else None
    if legacy_handle and not cleanup.get("handle"):
        cleanup["handle"] = legacy_handle
    runs = (
        await session.execute(
            select(ExperimentRun).where(
                ExperimentRun.experiment_id == experiment.id,
                ExperimentRun.status == "running",
            )
        )
    ).scalars().all()
    if not cleanup and not runs:
        if experiment.voyage_id is not None:
            from app.services import resource_leases

            await resource_leases.release_for_run(session, experiment.voyage_id)
        return True

    cleanup.update(status="pending")
    cleanup["pids"] = sorted(
        _positive_pids(cleanup.get("pids")) | {int(run.pid) for run in runs if run.pid}
    )
    expected_identity = _cleanup_identity(cleanup)
    state["remote_cleanup"] = cleanup
    experiment.iteration_state = state
    await session.commit()

    credential = (
        await session.get(SSHCredential, experiment.credential_id)
        if experiment.credential_id is not None else None
    )
    confirmed_runs: set[uuid.UUID] = set()
    confirmed_pids: set[int] = set()
    fully_confirmed = True
    stop_status = "credential_unavailable"
    executor = None
    try:
        if credential is None:
            fully_confirmed = False
        else:
            executor = await _open_cleanup_executor(experiment, credential)
            handle_data = cleanup.get("handle")
            handle = managed_handle_from_data(handle_data)
            intent = cleanup.get("operation_context")
            if handle is None and handle_data is None and isinstance(intent, dict):
                if intent.get("operation") not in {
                    "environment-prepare", "dependency-install", "application-smoke",
                    "plot-dependencies", "experiment-plot",
                }:
                    raise ValueError("invalid pending operation identity")
                executor.bind_run_workspace(cleanup.get("workspace_id"))
                recovered = await executor.recover_managed_command(OperationContext(
                    phase=str(intent["phase"]), operation=str(intent["operation"]),
                    display_command=str(intent["display_command"]),
                ))
                # An older terminal pointer cannot confirm a newly reserved launch.
                if recovered and recovered.attempt_id == cleanup.get("launch_attempt_id"):
                    handle = recovered
            if intent is not None and handle_data is None and handle is None:
                fully_confirmed = False
                stop_status = "launch_identity_unavailable"
            invalid_handle = handle_data is not None and handle is None
            if invalid_handle:
                fully_confirmed = False
                stop_status = "invalid_handle"
            elif handle is not None:
                handle_workspace = (
                    cleanup.get("workspace_id") if intent and handle_data is None
                    else await _bind_managed_workspace(session, executor, experiment, handle)
                )
                outcome = await executor.stop_managed_command(handle)
                stop_status = outcome.status
                fully_confirmed = bool(outcome)
                if outcome:
                    confirmed_pids.add(handle.process_id)

            targets = [(run, run.pid) for run in runs]
            targets.extend(
                (None, pid) for pid in cleanup["pids"]
                if not any(run.pid == pid for run in runs)
            )
            if handle is None and not targets:
                fully_confirmed = False
                stop_status = "identity_unavailable"
            for run, pid in targets:
                workspace_id = _run_workspace(experiment, run) if run is not None else None
                if handle is not None and (
                    (run is None and pid == handle.process_id)
                    or (
                        run is not None and handle.operation_id == "experiment-run"
                        and workspace_id == handle_workspace
                        and (pid == handle.process_id or (pid is None and workspace_id))
                    )
                ):
                    if handle.process_id in confirmed_pids:
                        if run is not None:
                            run.pid = handle.process_id
                            confirmed_runs.add(run.id)
                    else:
                        fully_confirmed = False
                    continue  # A refused managed stop cannot fall back to PID signalling.
                if invalid_handle:
                    continue
                executor.bind_run_workspace(workspace_id)
                recovered = await executor.recover_managed_command(
                    OperationContext(
                        phase="application.run",
                        operation="experiment-run",
                        display_command=run.command if run is not None else "bash run.sh",
                        target=experiment.server_host,
                        repair_scope=RepairScope.APPLICATION_FILES,
                    )
                )
                if recovered is not None:
                    if pid is not None and recovered.process_id != pid:
                        fully_confirmed = False
                        stop_status = "identity_changed"
                        continue
                    outcome = await executor.stop_managed_command(recovered)
                    stop_status = outcome.status
                    if not outcome:
                        fully_confirmed = False
                        continue
                    pid = recovered.process_id
                    if run is not None:
                        run.pid = pid
                elif workspace_id is not None or pid is None:
                    fully_confirmed = False
                    stop_status = "identity_unavailable"
                    continue
                else:
                    await executor.kill_pid(pid)
                    if await executor.check_pid(pid):
                        fully_confirmed = False
                        stop_status = "stop_unconfirmed"
                        continue
                    stop_status = "legacy_pid_exited"
                confirmed_pids.add(pid)
                if run is not None:
                    confirmed_runs.add(run.id)
    except Exception as exc:  # noqa: BLE001 - SSH unavailability does not undo local cancellation
        fully_confirmed = False
        stop_status = f"cleanup_unavailable:{type(exc).__name__}"
        logger.warning(
            "remote cleanup unavailable for experiment %s: %s", experiment.id, type(exc).__name__
        )
    finally:
        if executor is not None:
            try:
                await executor.close()
            except Exception:  # noqa: BLE001 - verified process termination is still valid
                logger.warning("remote cleanup connection close failed for %s", experiment.id)

    for run in runs:
        if run.id in confirmed_runs:
            run.status = "failed"
            run.exit_code = run.exit_code if run.exit_code is not None else -15
            run.finished_at = utcnow()
    # The API eagerly loads experiment.runs; refresh-expire cascades would
    # discard these verified run updates if they were not flushed first.
    await session.flush()
    # Remote I/O cannot clear a different attempt saved by a concurrent worker.
    await session.refresh(experiment)
    state = dict(experiment.iteration_state or {})
    current_cleanup = state.get("remote_cleanup") or {}
    if current_cleanup and _cleanup_identity(current_cleanup) != expected_identity:
        fully_confirmed = False
    elif fully_confirmed:
        state.pop("remote_cleanup", None)
        if voyage is not None:
            await session.refresh(voyage)
            checkpoint = dict(voyage.checkpoint or {})
            checkpoint.pop("managed_command_waiting", None)
            voyage.checkpoint = checkpoint
    else:
        # A partially completed cleanup must not signal already confirmed PIDs
        # again on a later retry; those IDs may have been reused meanwhile.
        cleanup["pids"] = sorted(_positive_pids(cleanup.get("pids")) - confirmed_pids)
        saved_handle = managed_handle_from_data(cleanup.get("handle"))
        if saved_handle is not None and saved_handle.process_id in confirmed_pids:
            cleanup.pop("handle", None)
            cleanup.pop("workspace_id", None)
        cleanup["stop_status"] = stop_status
        state["remote_cleanup"] = cleanup
    experiment.iteration_state = state
    await session.commit()
    if fully_confirmed and experiment.voyage_id is not None:
        from app.services import resource_leases

        await resource_leases.release_for_run(session, experiment.voyage_id)
    return fully_confirmed
