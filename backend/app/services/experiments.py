"""Experiment Lab 业务逻辑（不 import fastapi，docs/api-m4.md §2/§3）。

- 创建实验：校验 idea promoted + 凭据属本人 → Experiment 与 kind=experiment 的 voyage 1:1；
- 本地日志镜像：{data_dir}/experiments/<exp_id>/run_<seq>.log（logs API / SSE 只读本地镜像）；
- 取消：协作式 cancel voyage + 尽力 SSH kill 运行中的进程。
"""

import logging
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import get_settings
from app.models.activity import Activity
from app.models.experiment import EXPERIMENT_TERMINAL_STATUSES, Experiment, ExperimentRun
from app.models.idea import Idea
from app.models.project import Project, ProjectMember
from app.models.ssh_credential import SSHCredential
from app.models.voyage import TERMINAL_STATUSES, VoyageRun
from app.schemas.experiment import ExperimentCreate, ExperimentRead, ExperimentRunRead
from app.services import ssh_exec

logger = logging.getLogger("polaris.experiments")

DEFAULT_BUDGET: dict[str, Any] = {"max_hours": 4, "max_runs": 10}


class IdeaNotFoundError(Exception):
    """idea 不存在或不属于该项目。"""


class IdeaNotPromotedError(Exception):
    """idea 未晋级（status != promoted）。"""


class CredentialNotFoundError(Exception):
    """SSH 凭据不存在或不属于当前用户。"""


class ExperimentAlreadyFinishedError(Exception):
    """对终态实验执行 cancel。"""


# ---- 本地日志镜像 ----


def local_log_path(experiment_id: uuid.UUID | str, seq: int) -> Path:
    return Path(get_settings().data_dir) / "experiments" / str(experiment_id) / f"run_{seq}.log"


def append_local_log(experiment_id: uuid.UUID | str, seq: int, text: str) -> Path:
    path = local_log_path(experiment_id, seq)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(text)
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
    credential = await session.get(SSHCredential, data.credential_id)
    if credential is None or credential.user_id != user_id:
        raise CredentialNotFoundError(str(data.credential_id))

    params = data.params
    budget = dict(DEFAULT_BUDGET)
    if params and params.budget:
        budget |= params.budget.model_dump()

    experiment = Experiment(
        project_id=project.id,
        idea_id=idea.id,
        credential_id=credential.id,
        status="planning",
        budget=budget,
        server_host=credential.host,
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
        created_at=experiment.created_at,
        updated_at=experiment.updated_at,
    )


def serialize_runs(experiment: Experiment) -> list[ExperimentRunRead]:
    return [ExperimentRunRead.model_validate(r) for r in experiment.runs]


async def list_experiments(
    session: AsyncSession, *, project_id: uuid.UUID
) -> list[tuple[Experiment, str]]:
    stmt = (
        select(Experiment, Idea.title)
        .join(Idea, Idea.id == Experiment.idea_id)
        .where(Experiment.project_id == project_id)
        .order_by(Experiment.created_at.desc())
    )
    return [(exp, title) for exp, title in (await session.execute(stmt)).all()]


async def get_experiment_for_user(
    session: AsyncSession, *, experiment_id: uuid.UUID, user_id: uuid.UUID
) -> tuple[Experiment, str] | None:
    """取实验（含 runs）；非项目成员视为不存在（返回 None）。"""
    stmt = (
        select(Experiment, Idea.title)
        .join(Idea, Idea.id == Experiment.idea_id)
        .join(ProjectMember, ProjectMember.project_id == Experiment.project_id)
        .where(Experiment.id == experiment_id, ProjectMember.user_id == user_id)
        .options(selectinload(Experiment.runs))
    )
    row = (await session.execute(stmt)).first()
    return (row[0], row[1]) if row else None


def latest_run(experiment: Experiment) -> ExperimentRun | None:
    return max(experiment.runs, key=lambda r: r.seq, default=None)


# ---- 状态联动 ----


async def fail_by_voyage(session: AsyncSession, voyage_id: uuid.UUID) -> Experiment | None:
    """闸门驳回等场景：关联实验（非终态）置 failed，返回该实验。"""
    stmt = select(Experiment).where(Experiment.voyage_id == voyage_id)
    experiment = (await session.execute(stmt)).scalar_one_or_none()
    if experiment is None or experiment.status in EXPERIMENT_TERMINAL_STATUSES:
        return None
    experiment.status = "failed"
    await session.commit()
    await session.refresh(experiment)
    return experiment


# ---- 取消 ----


async def cancel_experiment(session: AsyncSession, experiment: Experiment) -> Experiment:
    """取消：voyage 置 cancelled（协作式）+ 运行中 run 置 failed + 尽力 kill 远端进程。

    先提交 DB 状态再做 SSH kill（审计写入用独立连接，避免持有未提交事务时死锁；
    kill 是尽力而为，SSH 不可达不阻塞取消）。
    """
    if experiment.status in EXPERIMENT_TERMINAL_STATUSES:
        raise ExperimentAlreadyFinishedError(str(experiment.id))

    if experiment.voyage_id is not None:
        voyage = await session.get(VoyageRun, experiment.voyage_id)
        if voyage is not None and voyage.status not in TERMINAL_STATUSES:
            voyage.status = "cancelled"

    stmt = select(ExperimentRun).where(
        ExperimentRun.experiment_id == experiment.id, ExperimentRun.status == "running"
    )
    runs = (await session.execute(stmt)).scalars().all()
    pids = [int(run.pid) for run in runs if run.pid]
    for run in runs:
        run.status = "failed"
    credential = (
        await session.get(SSHCredential, experiment.credential_id)
        if experiment.credential_id and pids
        else None
    )

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

    if credential is not None:
        await _kill_pids(credential, experiment, pids)
    return experiment


async def _kill_pids(credential: SSHCredential, experiment: Experiment, pids: list[int]) -> None:
    """尽力而为的远端 kill（DB 状态已提交后调用）。"""
    try:
        executor = await ssh_exec.open_executor(
            credential=credential,
            exp_id=str(experiment.id),
            project_id=experiment.project_id,
        )
    except Exception as e:  # noqa: BLE001 — kill 是尽力而为
        logger.warning("cancel: SSH 连接失败，跳过远端 kill：%s", e)
        return
    try:
        for pid in pids:
            try:
                await executor.kill_pid(pid)
            except Exception as e:  # noqa: BLE001
                logger.warning("cancel: kill pid=%s 失败：%s", pid, e)
    finally:
        await executor.close()
