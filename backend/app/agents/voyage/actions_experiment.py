"""experiment voyage 动作（kind ``experiment`` 固定五步计划的执行体，docs/api-m4.md §3）。

流水线：experiment.plan →（compute_budget 闸门）experiment.setup →
       experiment.smoke → experiment.run → experiment.report

约定：
- LLM 只产出 plan JSON / 代码文件内容 / 报告 markdown，远程命令一律走
  services/ssh_exec 的白名单模板（LLM 永远不拼 shell）；
- Experiment.status 与步骤联动（awaiting_gate/setup/running/reporting/done），
  每次流转发 WS ``experiment.status``；
- 步骤均声明 ``on_failure="fail"``：固定管线不重规划，失败即 voyage failed，
  动作内部先把 Experiment 置 failed 再抛错；
- 轮询循环在 experiment.run 内部（30s），每轮做协作式 cancel 检查、增量拉日志
  写本地镜像、解析 ``POLARIS_METRIC {json}`` 行、检查预算超时。
"""

import asyncio
import functools
import json
import re
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.voyage.actions import ActionContext, register
from app.core.db import get_sessionmaker
from app.core.llm.base import Message
from app.models.activity import Activity
from app.models.base import utcnow
from app.models.experiment import EXPERIMENT_TERMINAL_STATUSES, Experiment, ExperimentRun
from app.models.idea import Idea
from app.models.paper import Paper
from app.models.ssh_credential import SSHCredential
from app.models.voyage import VoyageRun
from app.services import experiments as experiments_service
from app.services import ssh_exec

RUN_POLL_SECONDS = 30.0  # 正式运行轮询间隔（测试 monkeypatch 为 0）
MAX_SMOKE_FIXES = 2  # 冒烟失败回 LLM 修代码的次数上限
_MAX_JSON_ATTEMPTS = 3  # 首次 + 重试 2 次
_WIKI_CONTEXT_PAPERS = 6
_WIKI_EXCERPT_CHARS = 600
_LOG_TAIL_FOR_REPORT = 60
_STDERR_CHARS = 2000

METRIC_LINE_RE = re.compile(r"POLARIS_METRIC\s+(\{.*\})")

PLAN_SYSTEM_PROMPT = """\
你是 Experiment Lab 的实验规划师，基于晋级 idea 与相关 wiki 摘要产出实验计划。
只输出一个 JSON 对象，不要输出任何其他文字或 Markdown 代码块，格式：
{"hypotheses": [{"text": "可检验的假设", "status": "testing"}],
 "repro_strategy": "基线复现策略（官方代码 > 可信第三方 > 自重写 > 仅引用数字）",
 "steps": ["实验步骤 1", "实验步骤 2"],
 "budget_estimate": {"gpu_hours": 2, "runs": 3}}
约束：hypotheses 1-5 条且必须可被实验证实/证伪；steps 3-8 条；
budget_estimate 是对象（至少含 gpu_hours）。
"""

CODE_SYSTEM_PROMPT = """\
你是 Experiment Lab 的实验工程师，为给定实验计划编写可直接运行的代码文件。
只输出一个 JSON 对象，不要输出任何其他文字或 Markdown 代码块，格式：
{"files": {"requirements.txt": "内容", "run.sh": "内容", "train.py": "内容"}}
硬约束：
- 必须包含 requirements.txt 与 run.sh；文件路径必须是相对路径（禁止 .. / 绝对路径 / ~）
- run.sh 必须支持 --smoke 参数（小样本/1 step 快速通过），并使用 .venv/bin/python 运行
- 训练/评估代码必须用 print('POLARIS_METRIC ' + json.dumps({"name": 指标名, "step": 步数, \
"value": 数值})) 输出关键指标
- 数据集只用小型公开数据或程序合成数据；不得读写工作目录以外的任何路径；不得访问网络下载大文件
"""

FIX_SYSTEM_PROMPT = (
    CODE_SYSTEM_PROMPT
    + """\

现在冒烟测试失败了，请根据报错修复代码：输出修复后的完整文件集合（同上 JSON 格式）。
"""
)

REPORT_SYSTEM_PROMPT = """\
你是 Experiment Lab 的报告撰写人。基于实验计划、指标数据与日志尾部撰写中文 markdown 报告，
以「## 实验报告」开头，包含：结果概览、指标表现、假设验证结论（逐条 verified/falsified/
testing）、局限与后续建议。直接输出 markdown，不要输出 JSON。
"""


# ---- 公共小件 ----


def _params(ctx: ActionContext) -> dict[str, Any]:
    params = (ctx.checkpoint or {}).get("params")
    return params if isinstance(params, dict) else {}


def _experiment_id(ctx: ActionContext) -> uuid.UUID:
    raw = _params(ctx).get("experiment_id")
    if not raw:
        raise ValueError("experiment voyage 缺少 checkpoint.params.experiment_id")
    return uuid.UUID(str(raw))


async def _get_experiment(session: AsyncSession, ctx: ActionContext) -> Experiment:
    experiment = await session.get(Experiment, _experiment_id(ctx))
    if experiment is None:
        raise ValueError(f"experiment not found: {_experiment_id(ctx)}")
    return experiment


async def _set_status(
    ctx: ActionContext, session: AsyncSession, experiment: Experiment, status: str
) -> None:
    if experiment.status == status:
        return
    experiment.status = status
    await session.commit()
    await ctx.notify(
        {"type": "experiment.status", "experiment_id": str(experiment.id), "status": status}
    )


async def _mark_failed(ctx: ActionContext, reason: str) -> None:
    """异常路径：实验（非终态）置 failed + WS + Activity。"""
    async with get_sessionmaker()() as session:
        experiment = await session.get(Experiment, _experiment_id(ctx))
        if experiment is None or experiment.status in EXPERIMENT_TERMINAL_STATUSES:
            return
        session.add(
            Activity(
                project_id=experiment.project_id,
                actor="agent:experiment",
                kind="experiment.failed",
                message=f"实验失败：{reason[:300]}",
                payload={"experiment_id": str(experiment.id), "reason": reason[:1000]},
            )
        )
        await _set_status(ctx, session, experiment, "failed")


def _guarded(func):
    """动作异常时先把实验置 failed 再抛给 helm（helm 记 observation.error）。"""

    @functools.wraps(func)
    async def wrapper(ctx: ActionContext, params: dict[str, Any]) -> dict[str, Any]:
        try:
            return await func(ctx, params)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            await _mark_failed(ctx, f"{type(e).__name__}: {e}")
            raise

    return wrapper


def _extract_json(content: str) -> Any:
    start = content.find("{")
    end = content.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object found")
    return json.loads(content[start : end + 1])


async def _complete_json(ctx: ActionContext, *, system: str, user: str, validate) -> Any:
    """stage=experiment 的 LLM JSON 请求：解析/校验失败重试，仍失败抛 ValueError。"""
    last_error: Exception | None = None
    for _attempt in range(_MAX_JSON_ATTEMPTS):
        result = await ctx.llm.complete(
            "experiment",
            [Message(role="system", content=system), Message(role="user", content=user)],
            user_id=ctx.run.created_by,
            project_id=ctx.run.project_id,
            voyage_id=ctx.run.id,
        )
        try:
            return validate(_extract_json(result.content))
        except (ValueError, TypeError, KeyError, json.JSONDecodeError) as e:
            last_error = e
    raise ValueError(f"LLM 连续输出非法 JSON：{last_error}")


async def _open_executor(session: AsyncSession, ctx: ActionContext, experiment: Experiment):
    if experiment.credential_id is None:
        raise ValueError("实验缺少 SSH 凭据（credential_id 为空）")
    credential = await session.get(SSHCredential, experiment.credential_id)
    if credential is None:
        raise ValueError("SSH 凭据已删除，无法连接实验服务器")
    return await ssh_exec.open_executor(
        credential=credential, exp_id=str(experiment.id), project_id=experiment.project_id
    )


# ---- 计划 schema 校验 ----

_HYP_STATUSES = ("testing", "verified", "falsified")


def validate_plan(data: Any) -> dict[str, Any]:
    """严格校验 plan JSON：hypotheses / repro_strategy / steps / budget_estimate 缺一不可。"""
    if not isinstance(data, dict):
        raise ValueError("plan payload is not an object")
    raw_hyps = data.get("hypotheses")
    if not isinstance(raw_hyps, list) or not raw_hyps:
        raise ValueError('expected non-empty "hypotheses" list')
    hypotheses = []
    for hyp in raw_hyps:
        text = hyp.get("text") if isinstance(hyp, dict) else hyp
        if not isinstance(text, str) or not text.strip():
            raise ValueError("hypothesis missing text")
        status = hyp.get("status") if isinstance(hyp, dict) else None
        hypotheses.append(
            {"text": text.strip(), "status": status if status in _HYP_STATUSES else "testing"}
        )
    repro = data.get("repro_strategy")
    if not isinstance(repro, str) or not repro.strip():
        raise ValueError('expected string "repro_strategy"')
    raw_steps = data.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        raise ValueError('expected non-empty "steps" list')
    steps = [str(s).strip() for s in raw_steps if str(s).strip()]
    if not steps:
        raise ValueError('expected non-empty "steps" list')
    budget = data.get("budget_estimate")
    if not isinstance(budget, dict) or not budget:
        raise ValueError('expected object "budget_estimate"')
    return {
        "hypotheses": hypotheses,
        "repro_strategy": repro.strip(),
        "steps": steps,
        "budget_estimate": budget,
    }


def validate_files(data: Any) -> dict[str, str]:
    """代码文件 dict 校验：requirements.txt / run.sh 必须存在，路径过白名单校验。"""
    files = data.get("files") if isinstance(data, dict) else None
    if not isinstance(files, dict) or not files:
        raise ValueError('expected {"files": {...}}')
    normalized: dict[str, str] = {}
    for name, content in files.items():
        rel = ssh_exec._validate_relpath(str(name))
        normalized[rel] = str(content)
    for required in ("requirements.txt", "run.sh"):
        if required not in normalized:
            raise ValueError(f"missing required file: {required}")
    if "--smoke" not in normalized["run.sh"]:
        raise ValueError("run.sh must support --smoke argument")
    return normalized


# ---- 指标解析 ----


def parse_metric_lines(text: str) -> list[dict[str, Any]]:
    """解析日志中的 ``POLARIS_METRIC {json}`` 行 → [{name, step, value}]。"""
    points: list[dict[str, Any]] = []
    for line in text.splitlines():
        m = METRIC_LINE_RE.search(line)
        if not m:
            continue
        try:
            data = json.loads(m.group(1))
        except json.JSONDecodeError:
            continue
        name = data.get("name") if isinstance(data, dict) else None
        value = data.get("value") if isinstance(data, dict) else None
        if not isinstance(name, str) or not isinstance(value, int | float):
            continue
        step = data.get("step")
        points.append(
            {
                "name": name,
                "step": int(step) if isinstance(step, int | float) else None,
                "value": float(value),
            }
        )
    return points


def merge_metrics(target: dict[str, Any] | None, points: list[dict[str, Any]]) -> dict[str, Any]:
    """把指标点合并进 {name: [{step, value}]}（返回新 dict，便于 JSON 列写回）。"""
    merged: dict[str, Any] = {k: list(v) for k, v in (target or {}).items()}
    for point in points:
        merged.setdefault(point["name"], []).append(
            {"step": point["step"], "value": point["value"]}
        )
    return merged


def _elapsed_hours(started_at: datetime | None) -> float:
    if started_at is None:
        return 0.0
    started = started_at if started_at.tzinfo else started_at.replace(tzinfo=UTC)
    return max(0.0, (utcnow() - started).total_seconds() / 3600.0)


# ---- 1. 计划（stage=experiment） ----


@register("experiment.plan")
@_guarded
async def experiment_plan(ctx: ActionContext, params: dict[str, Any]) -> dict[str, Any]:
    async with get_sessionmaker()() as session:
        experiment = await _get_experiment(session, ctx)
        idea = await session.get(Idea, experiment.idea_id)
        if idea is None:
            raise ValueError("实验关联的 idea 不存在")

        if not isinstance(experiment.plan, dict):  # 断点幂等
            papers = (
                (
                    await session.execute(
                        select(Paper)
                        .where(
                            Paper.project_id == experiment.project_id,
                            Paper.status.in_(("compiled", "included")),
                            Paper.wiki_content.is_not(None),
                        )
                        .order_by(Paper.relevance_score.desc().nulls_last(), Paper.created_at)
                        .limit(_WIKI_CONTEXT_PAPERS)
                    )
                )
                .scalars()
                .all()
            )
            wiki_context = (
                "\n\n".join(
                    f"### {p.title}\n{(p.wiki_content or '')[:_WIKI_EXCERPT_CHARS]}" for p in papers
                )
                or "（知识库为空）"
            )
            gpu_hint = _params(ctx).get("gpu_hint")
            user_prompt = (
                f"想法标题：{idea.title}\n"
                f"想法概述：{idea.summary or '（无）'}\n"
                f"想法详情：\n{(idea.content or '')[:4000]}\n\n"
                f"相关 wiki 摘要：\n{wiki_context}\n\n"
                f"预算约束：{json.dumps(experiment.budget or {}, ensure_ascii=False)}\n"
                f"GPU 提示：{gpu_hint or '（无）'}"
            )
            plan = await _complete_json(
                ctx, system=PLAN_SYSTEM_PROMPT, user=user_prompt, validate=validate_plan
            )
            experiment.plan = plan
            await session.commit()
        plan = experiment.plan

        # 预算闸门 payload（engine 建 Gate 时合并）：实验 id + 预算摘要 + 计划摘要
        ctx.checkpoint["gate_payload"] = {
            "experiment_id": str(experiment.id),
            "idea_title": idea.title,
            "budget": experiment.budget,
            "budget_estimate": plan.get("budget_estimate"),
            "plan_summary": {
                "hypotheses": [h["text"] for h in plan.get("hypotheses", [])],
                "repro_strategy": str(plan.get("repro_strategy", ""))[:300],
                "steps": len(plan.get("steps", [])),
            },
        }
        # 固定管线下一站是 compute_budget 闸门
        await _set_status(ctx, session, experiment, "awaiting_gate")

    return {
        "hypotheses": len(plan.get("hypotheses", [])),
        "steps": len(plan.get("steps", [])),
        "budget_estimate": plan.get("budget_estimate"),
    }


# ---- 2. 建环境（闸门后）：mkdir → LLM 代码生成 → 写文件 → venv ----


@register("experiment.setup")
@_guarded
async def experiment_setup(ctx: ActionContext, params: dict[str, Any]) -> dict[str, Any]:
    async with get_sessionmaker()() as session:
        experiment = await _get_experiment(session, ctx)
        await _set_status(ctx, session, experiment, "setup")

        files = ctx.checkpoint.get("exp_files")
        if not isinstance(files, dict):  # 断点幂等：已生成的代码不重复调 LLM
            user_prompt = (
                f"实验计划：{json.dumps(experiment.plan or {}, ensure_ascii=False)[:6000]}\n"
                f"预算：{json.dumps(experiment.budget or {}, ensure_ascii=False)}"
            )
            files = await _complete_json(
                ctx, system=CODE_SYSTEM_PROMPT, user=user_prompt, validate=validate_files
            )
            ctx.checkpoint["exp_files"] = files

        executor = await _open_executor(session, ctx, experiment)
        try:
            await executor.mkdir_workdir()
            experiment.workdir = executor.workdir
            experiment.server_host = executor.host
            await session.commit()
            written = await executor.write_files(files)
            venv = await executor.setup_venv()
        finally:
            await executor.close()
        if venv.exit_status != 0:
            raise RuntimeError(f"依赖安装失败（exit={venv.exit_status}）：{venv.stderr[-500:]}")

    return {"workdir": experiment.workdir, "files": written, "venv_exit": venv.exit_status}


# ---- 3. 冒烟测试：exit 0 通过；失败回 LLM 修文件（≤2 次） ----


@register("experiment.smoke")
@_guarded
async def experiment_smoke(ctx: ActionContext, params: dict[str, Any]) -> dict[str, Any]:
    async with get_sessionmaker()() as session:
        experiment = await _get_experiment(session, ctx)
        files: dict[str, str] = dict(ctx.checkpoint.get("exp_files") or {})

        executor = await _open_executor(session, ctx, experiment)
        try:
            attempts = 0
            fixes = 0
            while True:
                attempts += 1
                result = await executor.run_smoke()
                if result.exit_status == 0:
                    return {"exit_code": 0, "attempts": attempts, "fixes": fixes}
                if fixes >= MAX_SMOKE_FIXES:
                    raise RuntimeError(
                        f"冒烟测试连续失败（{attempts} 次，exit={result.exit_status}）："
                        f"{(result.stderr or result.stdout)[-500:]}"
                    )
                # 把 stderr 回给 LLM 修文件
                fixes += 1
                user_prompt = (
                    f"当前文件：{json.dumps(files, ensure_ascii=False)[:8000]}\n\n"
                    f"冒烟测试退出码：{result.exit_status}\n"
                    f"stderr：\n{(result.stderr or result.stdout)[-_STDERR_CHARS:]}"
                )
                files = await _complete_json(
                    ctx, system=FIX_SYSTEM_PROMPT, user=user_prompt, validate=validate_files
                )
                ctx.checkpoint["exp_files"] = files
                await executor.write_files(files)
        finally:
            await executor.close()


# ---- 4. 正式运行：launch + 轮询（cancel / 日志镜像 / 指标 / 预算超时） ----


@register("experiment.run")
@_guarded
async def experiment_run(ctx: ActionContext, params: dict[str, Any]) -> dict[str, Any]:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        experiment = await _get_experiment(session, ctx)
        await _set_status(ctx, session, experiment, "running")
        budget = experiment.budget or {}
        max_hours = float(budget.get("max_hours") or 0)
        max_runs = int(budget.get("max_runs") or 0)

        existing = (
            (
                await session.execute(
                    select(ExperimentRun.seq).where(ExperimentRun.experiment_id == experiment.id)
                )
            )
            .scalars()
            .all()
        )
        seq = (max(existing) + 1) if existing else 1
        if max_runs and seq > max_runs:
            raise RuntimeError(f"超出预算 max_runs={max_runs}，拒绝启动第 {seq} 次运行")

        executor = await _open_executor(session, ctx, experiment)
        try:
            pid, command = await executor.launch_run()
            log_path = experiments_service.append_local_log(experiment.id, seq, "")
            run = ExperimentRun(
                experiment_id=experiment.id,
                seq=seq,
                command=command,
                status="running",
                pid=pid,
                log_path=str(log_path),
                started_at=utcnow(),
            )
            session.add(run)
            await session.commit()
            await session.refresh(run)

            observation = await _poll_run(ctx, session, executor, experiment, run, max_hours)
        finally:
            await executor.close()
    return observation


async def _poll_run(
    ctx: ActionContext,
    session: AsyncSession,
    executor: ssh_exec.SSHExecutor,
    experiment: Experiment,
    run: ExperimentRun,
    max_hours: float,
) -> dict[str, Any]:
    offset = 0

    async def ingest_chunk() -> None:
        nonlocal offset
        chunk, offset = await executor.tail_log(offset)
        if not chunk:
            return
        experiments_service.append_local_log(experiment.id, run.seq, chunk)
        points = parse_metric_lines(chunk)
        if points:
            run.metrics = merge_metrics(run.metrics, points)
            experiment.metrics = merge_metrics(experiment.metrics, points)
        await session.commit()

    async def finish(exit_code: int | None) -> dict[str, Any]:
        await ingest_chunk()  # 收尾：抓最后一段日志
        run.exit_code = exit_code
        run.status = "succeeded" if exit_code == 0 else "failed"
        run.finished_at = utcnow()
        await session.commit()
        return {
            "run_id": str(run.id),
            "seq": run.seq,
            "exit_code": exit_code,
            "run_status": run.status,
            "metric_names": sorted((run.metrics or {}).keys()),
        }

    while True:
        # 协作式取消：每轮查 voyage 状态（cancel API 会把 voyage 置 cancelled）
        voyage_status = (
            await session.execute(select(VoyageRun.status).where(VoyageRun.id == ctx.run.id))
        ).scalar_one()
        if voyage_status == "cancelled":
            await executor.kill_pid(int(run.pid or 0))
            await ingest_chunk()
            run.status = "failed"
            run.finished_at = utcnow()
            await session.commit()
            await session.refresh(experiment)
            if experiment.status not in EXPERIMENT_TERMINAL_STATUSES:
                await _set_status(ctx, session, experiment, "cancelled")
            return {"cancelled": True, "run_id": str(run.id), "seq": run.seq}

        await ingest_chunk()

        exit_code = await executor.read_exit_code()
        if exit_code is not None:
            return await finish(exit_code)

        alive = await executor.check_pid(int(run.pid or 0))
        if not alive:
            # 进程没了但还没读到退出码：再读一次（竞态），仍无则按 failed 收尾
            return await finish(await executor.read_exit_code())

        if max_hours >= 0 and _elapsed_hours(run.started_at) > max_hours:
            await executor.kill_pid(int(run.pid or 0))
            await ingest_chunk()
            run.status = "failed"
            run.finished_at = utcnow()
            await session.commit()
            raise RuntimeError(f"运行超出预算 max_hours={max_hours}，已 kill（pid={run.pid}）")

        await asyncio.sleep(RUN_POLL_SECONDS)


# ---- 5. 报告（stage=experiment） ----


@register("experiment.report")
@_guarded
async def experiment_report(ctx: ActionContext, params: dict[str, Any]) -> dict[str, Any]:
    async with get_sessionmaker()() as session:
        experiment = await _get_experiment(session, ctx)
        await _set_status(ctx, session, experiment, "reporting")

        runs = (
            (
                await session.execute(
                    select(ExperimentRun)
                    .where(ExperimentRun.experiment_id == experiment.id)
                    .order_by(ExperimentRun.seq)
                )
            )
            .scalars()
            .all()
        )
        last_run = runs[-1] if runs else None
        log_lines, _ = experiments_service.read_local_log_tail(
            last_run.log_path if last_run else None, _LOG_TAIL_FOR_REPORT
        )
        runs_brief = [{"seq": r.seq, "status": r.status, "exit_code": r.exit_code} for r in runs]
        user_prompt = (
            f"实验计划：{json.dumps(experiment.plan or {}, ensure_ascii=False)[:4000]}\n"
            f"运行结果：{json.dumps(runs_brief, ensure_ascii=False)}\n"
            f"指标数据：{json.dumps(experiment.metrics or {}, ensure_ascii=False)[:4000]}\n"
            f"日志尾部：\n" + "\n".join(log_lines)
        )
        result = await ctx.llm.complete(
            "experiment",
            [
                Message(role="system", content=REPORT_SYSTEM_PROMPT),
                Message(role="user", content=user_prompt),
            ],
            user_id=ctx.run.created_by,
            project_id=ctx.run.project_id,
            voyage_id=ctx.run.id,
        )
        experiment.report = result.content.strip()
        run_ok = last_run is not None and last_run.status == "succeeded"
        final_status = "done" if run_ok else "failed"
        session.add(
            Activity(
                project_id=experiment.project_id,
                actor="agent:experiment",
                kind="experiment.completed",
                message=f"实验报告已生成（最终状态 {final_status}）",
                payload={"experiment_id": str(experiment.id), "final_status": final_status},
            )
        )
        await _set_status(ctx, session, experiment, final_status)

    return {
        "report_chars": len(experiment.report or ""),
        "final_status": final_status,
        "usage": result.usage,
    }
