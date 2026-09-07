"""BYO runner tier-1（#685，设计报告 §15）：SSH 主机的形式化接入。

三件事（不 import fastapi）：

1. **runner agent 版本钉定**（学 VS Code Remote-SSH：用户只给地址，安装/升级自动）。
   ``ensure_agent`` 首连比对远端 ``~/.polaris-runner/VERSION``，不符则推送 agent 载荷。
   现状取舍：仓库里原本**没有**独立的 agent 推送逻辑——远端辅助命令散落在
   ssh_exec 的固定模板里（探测/workdir/run.exit 约定各一摊）。本 PR 把这些约定
   收敛成一小组远端脚本作为 agent 载荷推上去并钉版本；**执行路径本期不切换**
   （固定模板命令继续直跑，行为零变化），脚本先作为版本化的远端契约落位，
   tier-2 的常驻 agent 与后续「命令走脚本」的收敛都在这个目录上迭代。

2. **runner 主机注册**：注册一台 runner 机器 = 建一个 host 类 Resource（#680），
   credential_id 关联 SSH 凭据——租约互斥/容量计数天然生效。新建主机默认
   ``config.ephemeral=true``（容器化执行不残留，GitHub runner 事故教训）；
   只影响新建，存量资源与现有 run 行为不动。

3. **凭据吊销联动**：删除 connection_credential 前先查活跃引用——有未终态实验
   引用则拒绝（API 层转 409）。选 409 而非级联 cancel：吊销是安全操作，但替用户
   杀正在跑的实验副作用太大，让用户先自己处置活跃 run 再吊销，语义最保守。
   吊销时对应 host Resource 标记不可用（config.unavailable，不删资源——
   租约历史与配置留着，换绑新凭据即可复活）。
"""

from __future__ import annotations

import logging
import uuid
from typing import Protocol

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.experiment import EXPERIMENT_TERMINAL_STATUSES, Experiment
from app.models.resource import Resource
from app.models.ssh_credential import ConnectionCredential

logger = logging.getLogger("polaris.byo_runner")

# ---------------------------------------------------------------------------
# Agent 版本钉定
# ---------------------------------------------------------------------------

# agent 载荷版本：脚本内容变了就 +1（远端按整包重推，不做逐文件 diff——载荷极小，
# 整包幂等重推比精细比对更不容易出「半新半旧」状态）。
AGENT_VERSION = "1"
AGENT_DIR_SHELL = "~/.polaris-runner"  # shell 命令用（~ 由远端展开）
AGENT_DIR_SFTP = ".polaris-runner"  # SFTP 相对 home 目录
_VERSION_PATH = f"{AGENT_DIR_SHELL}/VERSION"
_CMD_TIMEOUT = 60.0

# agent 载荷 = 一组远端辅助脚本：把 ssh_exec 散落固定模板里的三类约定
# （环境探测 / workdir 管理 / run.exit 落盘约定）收敛成可版本化的脚本。
# 内容全部是固定文本（无任何运行期拼接），不构成新的注入面。
AGENT_SCRIPTS: dict[str, str] = {
    # 环境探测：与 ssh_exec.probe_sysinfo 的四条探测命令同源（CPU/内存/磁盘/GPU），
    # 单脚本一次拿全，供 tier-2 agent 注册时自述硬件、也供人肉登录排查。
    "probe.sh": (
        "#!/usr/bin/env bash\n"
        "# polaris-runner agent: environment probe (fixed template, no arguments)\n"
        "echo '== cpu =='\n"
        "nproc 2>/dev/null; cat /proc/loadavg 2>/dev/null\n"
        "echo '== mem =='\n"
        "LC_ALL=C free -m 2>/dev/null\n"
        "echo '== disk =='\n"
        "LC_ALL=C df -PB1M -x tmpfs -x devtmpfs -x overlay -x squashfs 2>/dev/null"
        " | tail -n +2\n"
        "echo '== gpu =='\n"
        "nvidia-smi --query-gpu=index,memory.total,memory.free"
        " --format=csv,noheader,nounits 2>/dev/null || true\n"
    ),
    # workdir 管理：实验工作区固定在 ~/polaris_runs/<uuid>；参数强校验 UUID 前缀，
    # 与 ssh_exec.validate_exp_id 的白名单口径一致。
    "workdir.sh": (
        "#!/usr/bin/env bash\n"
        "# polaris-runner agent: workdir management under ~/polaris_runs\n"
        "# usage: workdir.sh ensure|clean <experiment-uuid>\n"
        "set -e\n"
        "action=$1; exp=$2\n"
        "case $exp in\n"
        "  [0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]-*) ;;\n"
        "  *) echo 'invalid experiment id' >&2; exit 64;;\n"
        "esac\n"
        "case $action in\n"
        "  ensure) mkdir -p ~/polaris_runs/$exp;;\n"
        "  clean) rm -rf ~/polaris_runs/$exp;;\n"
        "  *) echo 'unknown action' >&2; exit 64;;\n"
        "esac\n"
    ),
    # run.exit 约定：入口命令的退出码落 <workdir>/run.exit（依赖安装同理 setup.exit），
    # 轮询端只认这两个文件——脚本把该约定固化下来供 tier-2 复用。
    "run-status.sh": (
        "#!/usr/bin/env bash\n"
        "# polaris-runner agent: read the run.exit/setup.exit convention\n"
        "# usage: run-status.sh <experiment-uuid> [run|setup]\n"
        "exp=$1; kind=${2:-run}\n"
        "case $kind in run|setup) ;; *) echo 'unknown kind' >&2; exit 64;; esac\n"
        "cat ~/polaris_runs/$exp/$kind.exit 2>/dev/null\n"
    ),
}


class _SSHSessionLike(Protocol):
    """ensure_agent 只需要「跑命令 + SFTP 写文件」两个原语（避免反向 import ssh_exec）。"""

    async def run(self, command: str, timeout: float | None = None): ...

    async def write_file(self, path: str, content: str) -> None: ...


async def ensure_agent(session: _SSHSessionLike) -> bool:
    """远端 agent 就位保证：VERSION 一致则跳过；缺失/过期则整包重推。

    返回是否发生了推送。首连（无 VERSION 文件）自动装；后续连接一条 ``cat``
    比对即返回，代价可忽略。VERSION 最后写（tmp+mv 原子落盘）：推送中途断连
    只会留下无版本号的半成品，下次连接必然重推，不会出现「版本号新、脚本旧」。
    """
    probe = await session.run(f"cat {_VERSION_PATH} 2>/dev/null", timeout=_CMD_TIMEOUT)
    lines = (probe.stdout or "").strip().splitlines()
    if probe.exit_status == 0 and lines and lines[-1].strip() == AGENT_VERSION:
        return False
    await session.run(f"mkdir -p {AGENT_DIR_SHELL}/agent", timeout=_CMD_TIMEOUT)
    for name, content in AGENT_SCRIPTS.items():
        await session.write_file(f"{AGENT_DIR_SFTP}/agent/{name}", content)
    await session.run(f"chmod 700 {AGENT_DIR_SHELL}/agent/*.sh", timeout=_CMD_TIMEOUT)
    await session.run(
        f"printf '%s\\n' {AGENT_VERSION} > {_VERSION_PATH}.tmp"
        f" && mv {_VERSION_PATH}.tmp {_VERSION_PATH}",
        timeout=_CMD_TIMEOUT,
    )
    logger.info("byo_runner.agent pushed version=%s", AGENT_VERSION)
    return True


# ---------------------------------------------------------------------------
# runner 主机注册（host 类 Resource）
# ---------------------------------------------------------------------------


class RunnerHostCredentialError(Exception):
    """凭据不存在/不属于该用户（API 层转 404，不泄露存在性）。"""


class RunnerHostKindError(Exception):
    """凭据 kind 不是 ssh（tier-1 只吃 SSH 直连；API 层转 422）。"""


async def register_runner_host(
    session: AsyncSession,
    *,
    owner_id: uuid.UUID,
    name: str,
    credential_id: uuid.UUID,
    config: dict | None = None,
) -> Resource:
    """注册一台 BYO runner 机器 = 建 host 类 Resource 并关联 SSH 凭据。

    - 默认 exclusive=True：一台裸机同一时刻只跑一个实验（租约互斥，#680）；
      要并发席位的用户走 PATCH /resources 调 capacity/exclusive；
    - config.ephemeral 默认 True = 推荐容器化执行不残留（plan.container 路径）；
      显式传 False 表示接受裸机 venv 直跑（non-ephemeral，产物留在主机上）。
      该默认值只作用于新注册的主机，不改任何现有 run 的行为。
    """
    credential = await session.get(ConnectionCredential, credential_id)
    if credential is None or credential.user_id != owner_id:
        raise RunnerHostCredentialError(str(credential_id))
    if credential.kind != "ssh":
        raise RunnerHostKindError(
            f"runner host requires an ssh credential, got kind={credential.kind!r}"
        )
    merged: dict = {"ephemeral": True}
    merged.update(config or {})
    resource = Resource(
        owner_id=owner_id,
        name=name,
        kind="host",
        capacity=1,
        exclusive=True,
        credential_id=credential.id,
        config=merged,
    )
    session.add(resource)
    await session.commit()
    await session.refresh(resource)
    return resource


# ---------------------------------------------------------------------------
# 凭据吊销联动
# ---------------------------------------------------------------------------


class CredentialInUseError(Exception):
    """有未终态实验仍引用该凭据（API 层转 409）。"""

    def __init__(self, active_count: int) -> None:
        self.active_count = active_count
        super().__init__(f"credential referenced by {active_count} active experiment(s)")


async def revoke_connection_credential(
    session: AsyncSession, credential: ConnectionCredential
) -> None:
    """吊销（删除）连接凭据：活跃引用拒绝，host Resource 联动标记不可用。

    为什么 409 而非级联 cancel：见模块 docstring。资源上显式清 credential_id
    而不只靠 FK 的 SET NULL——sqlite（dev/测试）默认不开外键级联，显式写一遍
    两种库行为才一致。
    """
    active = (
        await session.execute(
            select(func.count())
            .select_from(Experiment)
            .where(
                Experiment.credential_id == credential.id,
                Experiment.status.notin_(EXPERIMENT_TERMINAL_STATUSES),
            )
        )
    ).scalar_one()
    if int(active):
        raise CredentialInUseError(int(active))

    resources = (
        (await session.execute(select(Resource).where(Resource.credential_id == credential.id)))
        .scalars()
        .all()
    )
    for resource in resources:
        merged = dict(resource.config or {})
        merged["unavailable"] = True
        merged["unavailable_reason"] = "credential_revoked"
        resource.config = merged
        resource.credential_id = None
    if resources:
        logger.info(
            "byo_runner.credential_revoked credential=%s resources_marked=%d",
            credential.id,
            len(resources),
        )
    await session.delete(credential)
    await session.commit()
