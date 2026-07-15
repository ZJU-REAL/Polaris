"""experiment voyage 动作（kind ``experiment`` 固定管线的执行体，docs/api-m5-a.md §1）。

流水线：experiment.plan →（compute_budget 闸门）experiment.setup →
       experiment.smoke → experiment.iterate → experiment.figures → experiment.report

约定：
- LLM 只产出 plan JSON / 代码文件内容 / reflection JSON / 绘图脚本 / 报告 markdown，
  远程命令一律走 services/ssh_exec 的白名单模板（LLM 永远不拼 shell）；
- Experiment.status 与步骤联动（awaiting_gate/setup/running/reporting/done），
  每次流转发 WS ``experiment.status``；
- 步骤均声明 ``on_failure="fail"``：固定管线不重规划，失败即 voyage failed，
  动作内部先把 Experiment 置 failed 再抛错；
- experiment.iterate 内部多轮循环：每轮 launch run → 轮询（30s，协作式 cancel /
  日志镜像 / POLARIS_METRIC + 可选 metrics.json 解析 / 预算超时）→ 主指标
  direction 感知比较 → LLM structured reflection → 假设回写 → decision 分支
  improve/debug/stop；iteration_state 持续落库，checkpoint 记轮次进度断点安全；
- experiment.figures：平台写 metrics_all.json → LLM 绘图脚本（只准读该文件）→
  白名单 run_plot → 拉回 figures/*.png(+.pdf) → VLM 质检（失败修脚本 ≤2 次）。
"""

import asyncio
import functools
import json
import re
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, update
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
MAX_DEBUG_FIXES = 3  # 迭代内 debug 分支独立限额（docs/api-m5-a.md §1）
MAX_FIGURE_FIXES = 2  # 绘图脚本执行失败 / VLM 质检不合格的修复次数上限
DEFAULT_NO_IMPROVE_STOP = 2  # 连续 N 轮主指标无提升即停（budget.no_improve_stop 可覆盖）
MAX_QC_IMAGES = 8  # 单次质检最多送 LLM 的图数
MAX_IMAGE_BYTES = 4 * 1024 * 1024  # 单图超过 4MB 不送 LLM（与 figure_annotate 一致）
_MAX_JSON_ATTEMPTS = 3  # 首次 + 重试 2 次
_WIKI_CONTEXT_PAPERS = 6
_WIKI_EXCERPT_CHARS = 600
_LOG_TAIL_FOR_REPORT = 60
_LOG_TAIL_FOR_REFLECTION = 40
_STDERR_CHARS = 2000

METRIC_LINE_RE = re.compile(r"POLARIS_METRIC\s+(\{.*\})")
_FIGURE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")  # 远端文件名白名单（防目录穿越）

PLAN_SYSTEM_PROMPT = """\
你是 Experiment Lab 的实验规划师，基于晋级 idea 与相关 wiki 摘要产出实验计划。
只输出一个 JSON 对象，不要输出任何其他文字或 Markdown 代码块，格式：
{"hypotheses": [{"text": "可检验的假设", "status": "testing"}],
 "repro_strategy": "基线复现策略（官方代码 > 可信第三方 > 自重写 > 仅引用数字）",
 "steps": ["实验步骤 1", "实验步骤 2"],
 "primary_metric": {"name": "主指标名", "direction": "maximize"},
 "budget_estimate": {"gpu_hours": 2, "runs": 3}}
约束：hypotheses 1-5 条且必须可被实验证实/证伪；steps 3-8 条；
primary_metric 必填：name 是训练代码 POLARIS_METRIC 输出的指标名，
direction 只能取 maximize（越大越好）或 minimize（越小越好）；
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

IMPROVE_SYSTEM_PROMPT = (
    CODE_SYSTEM_PROMPT
    + """\

现在进入自动迭代：上一轮运行已结束，请按 reflection 给出的改进计划（planned_change）
修改代码/超参，输出修改后的完整文件集合（同上 JSON 格式）。只做说明中的修改，不要重写无关部分。
"""
)

DEBUG_SYSTEM_PROMPT = (
    CODE_SYSTEM_PROMPT
    + """\

现在自动迭代中的正式运行失败了，请根据错误信息修复代码：输出修复后的完整文件集合（同上 JSON 格式）。
"""
)

REFLECTION_SYSTEM_PROMPT = """\
你是 Experiment Lab 的实验分析师，基于本轮运行结果做结构化反思并决定下一步。
只输出一个 JSON 对象，不要输出任何其他文字或 Markdown 代码块，格式：
{"observation": "本轮结果观察", "diagnosis": "原因诊断",
 "hypothesis_updates": [{"index": 0, "status": "verified|falsified|testing", "evidence": "证据"}],
 "decision": "improve|debug|stop", "planned_change": "下一轮计划修改", "stop_reason": null}
约束：
- hypothesis_updates 的 index 是假设清单下标（从 0 开始），status 只能取 verified/falsified/testing
- 本轮运行失败（exit_code 非 0）时 decision 用 debug；结果已足以回答全部假设时用 stop
- decision=stop 时 stop_reason 必填一句话；decision=improve 时 planned_change 必填
"""

PLOT_SYSTEM_PROMPT = """\
你是 Experiment Lab 的绘图工程师，为实验结果编写 matplotlib 绘图脚本。
只输出一个 JSON 对象，不要输出任何其他文字或 Markdown 代码块，格式：
{"files": {"plot_figures.py": "脚本内容"}}
硬约束：
- 脚本只准读取当前目录的 metrics_all.json（平台已把全部 run 的解析指标写入该文件），
  禁止硬编码任何数据点、禁止读取其他文件、禁止访问网络
- 使用 matplotlib 的 Agg 后端；图表输出到 figures/ 目录（脚本内自行创建），
  每张图同时保存 .png 与同名 .pdf（论文用）
- 每张图必须有标题与坐标轴标签，多序列时必须有图例，保证可读性
"""

FIGURE_QC_SYSTEM_PROMPT = """\
你是 Experiment Lab 的图表质检员，检查附带的实验图表是否合格：
坐标轴与刻度标签清晰、多序列有图例、内容可读且非空白。
只输出一个 JSON 对象，不要输出任何其他文字或 Markdown 代码块，格式：
{"passed": true, "figures": [{"index": 0, "caption": "一句中文图注"}], "issues": []}
index 对应附带图片顺序（从 0 开始）；不合格时 passed 置 false 并在 issues 里列出具体问题。
"""

REPORT_SYSTEM_PROMPT = """\
你是 Experiment Lab 的报告撰写人。基于实验计划、迭代过程、指标数据与日志尾部撰写中文 markdown 报告，
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
_PM_DIRECTIONS = ("maximize", "minimize")
_DECISIONS = ("improve", "debug", "stop")


def validate_plan(data: Any) -> dict[str, Any]:
    """严格校验 plan JSON：hypotheses / repro_strategy / steps / primary_metric /
    budget_estimate 缺一不可（primary_metric 为 docs/api-m5-a.md §1 新增必填）。"""
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
        item = {"text": text.strip(), "status": status if status in _HYP_STATUSES else "testing"}
        evidence = hyp.get("evidence") if isinstance(hyp, dict) else None
        if isinstance(evidence, str) and evidence.strip():
            item["evidence"] = evidence.strip()
        hypotheses.append(item)
    repro = data.get("repro_strategy")
    if not isinstance(repro, str) or not repro.strip():
        raise ValueError('expected string "repro_strategy"')
    raw_steps = data.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        raise ValueError('expected non-empty "steps" list')
    steps = [str(s).strip() for s in raw_steps if str(s).strip()]
    if not steps:
        raise ValueError('expected non-empty "steps" list')
    pm = data.get("primary_metric")
    if not isinstance(pm, dict):
        raise ValueError('expected object "primary_metric" with {name, direction}')
    pm_name = pm.get("name")
    if not isinstance(pm_name, str) or not pm_name.strip():
        raise ValueError("primary_metric missing name")
    pm_direction = pm.get("direction")
    if pm_direction not in _PM_DIRECTIONS:
        raise ValueError("primary_metric direction must be maximize|minimize")
    budget = data.get("budget_estimate")
    if not isinstance(budget, dict) or not budget:
        raise ValueError('expected object "budget_estimate"')
    return {
        "hypotheses": hypotheses,
        "repro_strategy": repro.strip(),
        "steps": steps,
        "primary_metric": {"name": pm_name.strip(), "direction": pm_direction},
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


def validate_reflection(data: Any) -> dict[str, Any]:
    """structured reflection 严格校验（docs/api-m5-a.md §1）。"""
    if not isinstance(data, dict):
        raise ValueError("reflection payload is not an object")
    observation = data.get("observation")
    diagnosis = data.get("diagnosis")
    if not isinstance(observation, str) or not observation.strip():
        raise ValueError('expected string "observation"')
    if not isinstance(diagnosis, str) or not diagnosis.strip():
        raise ValueError('expected string "diagnosis"')
    raw_updates = data.get("hypothesis_updates")
    if raw_updates is None:
        raw_updates = []
    if not isinstance(raw_updates, list):
        raise ValueError('"hypothesis_updates" must be a list')
    updates: list[dict[str, Any]] = []
    for upd in raw_updates:
        if not isinstance(upd, dict):
            raise ValueError("hypothesis_update is not an object")
        index = upd.get("index")
        if not isinstance(index, int) or isinstance(index, bool) or index < 0:
            raise ValueError("hypothesis_update index must be a non-negative int")
        status = upd.get("status")
        if status not in _HYP_STATUSES:
            raise ValueError("hypothesis_update status must be verified|falsified|testing")
        evidence = upd.get("evidence")
        updates.append(
            {
                "index": index,
                "status": status,
                "evidence": str(evidence).strip() if evidence else "",
            }
        )
    decision = data.get("decision")
    if decision not in _DECISIONS:
        raise ValueError("decision must be improve|debug|stop")
    planned_change = data.get("planned_change")
    stop_reason = data.get("stop_reason")
    return {
        "observation": observation.strip(),
        "diagnosis": diagnosis.strip(),
        "hypothesis_updates": updates,
        "decision": decision,
        "planned_change": str(planned_change).strip() if planned_change else None,
        "stop_reason": str(stop_reason).strip() if stop_reason else None,
    }


def validate_plot_files(data: Any) -> dict[str, str]:
    """绘图脚本校验：只接受 plot_figures.py 一个文件，且必须引用 metrics_all.json。"""
    files = data.get("files") if isinstance(data, dict) else None
    if not isinstance(files, dict) or not files:
        raise ValueError('expected {"files": {"plot_figures.py": ...}}')
    content = files.get("plot_figures.py")
    if not isinstance(content, str) or not content.strip():
        raise ValueError("missing required file: plot_figures.py")
    if "metrics_all.json" not in content:
        raise ValueError("plot_figures.py must read metrics_all.json (hard constraint)")
    return {"plot_figures.py": content}


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


def parse_metrics_json(text: str) -> list[dict[str, Any]]:
    """解析可选 workdir/metrics.json → 指标点列表（非法内容一律返回空，不抛错）。

    支持 {"name": 数值} 与 {"name": [{"step": 1, "value": 0.5}]} 两种形态。
    """
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, dict):
        return []
    points: list[dict[str, Any]] = []
    for name, value in data.items():
        if not isinstance(name, str):
            continue
        if isinstance(value, int | float) and not isinstance(value, bool):
            points.append({"name": name, "step": None, "value": float(value)})
        elif isinstance(value, list):
            for item in value:
                if not isinstance(item, dict):
                    continue
                v = item.get("value")
                if not isinstance(v, int | float) or isinstance(v, bool):
                    continue
                step = item.get("step")
                points.append(
                    {
                        "name": name,
                        "step": int(step) if isinstance(step, int | float) else None,
                        "value": float(v),
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


def extract_primary_value(metrics: dict[str, Any] | None, metric_name: str) -> float | None:
    """从 run.metrics 取主指标最后一个值（无该指标返回 None）。"""
    series = (metrics or {}).get(metric_name)
    if not isinstance(series, list) or not series:
        return None
    value = series[-1].get("value") if isinstance(series[-1], dict) else None
    return float(value) if isinstance(value, int | float) else None


def is_improvement(value: float, best: float | None, direction: str) -> bool:
    """direction 感知比较：maximize 越大越好，minimize 越小越好。"""
    if best is None:
        return True
    return value > best if direction == "maximize" else value < best


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
                "primary_metric": plan.get("primary_metric"),
                "steps": len(plan.get("steps", [])),
            },
        }
        # 固定管线下一站是 compute_budget 闸门
        await _set_status(ctx, session, experiment, "awaiting_gate")

    return {
        "hypotheses": len(plan.get("hypotheses", [])),
        "steps": len(plan.get("steps", [])),
        "primary_metric": plan.get("primary_metric"),
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
            # pip/venv 的报错可能走 stdout 或 stderr，两路都带上便于定位
            detail = (venv.stderr.strip() or venv.stdout.strip())[-600:]
            if not detail:
                detail = "（无输出，多为连接中断或超时）"
            raise RuntimeError(f"依赖安装失败（exit={venv.exit_status}）：{detail}")

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


# ---- 4. 自动迭代：多轮 launch + 轮询 + reflection + improve/debug/stop ----


async def _voyage_cancelled(session: AsyncSession, ctx: ActionContext) -> bool:
    status = (
        await session.execute(select(VoyageRun.status).where(VoyageRun.id == ctx.run.id))
    ).scalar_one()
    return status == "cancelled"


async def _persist_checkpoint(session: AsyncSession, ctx: ActionContext) -> None:
    """轮次进度落 VoyageRun.checkpoint（断点安全：worker 崩溃后已完成轮次不重跑）。"""
    await session.execute(
        update(VoyageRun).where(VoyageRun.id == ctx.run.id).values(checkpoint=dict(ctx.checkpoint))
    )
    await session.commit()


def _apply_hypothesis_updates(
    plan: dict[str, Any], updates: list[dict[str, Any]]
) -> dict[str, Any]:
    """假设回写：status（+evidence）写回 plan.hypotheses（返回新 dict 触发 JSON 列更新）。"""
    new_plan = dict(plan)
    hyps = [dict(h) for h in new_plan.get("hypotheses", [])]
    for upd in updates:
        index = upd["index"]
        if 0 <= index < len(hyps):
            hyps[index]["status"] = upd["status"]
            if upd.get("evidence"):
                hyps[index]["evidence"] = upd["evidence"]
    new_plan["hypotheses"] = hyps
    return new_plan


def _iteration_state(experiment: Experiment) -> dict[str, Any]:
    state = experiment.iteration_state or {}
    return {
        "no_improve_streak": int(state.get("no_improve_streak") or 0),
        "debug_count": int(state.get("debug_count") or 0),
        "stopped_reason": state.get("stopped_reason"),
    }


def _best_primary_value(runs: list[ExperimentRun], direction: str) -> float | None:
    values = [r.primary_value for r in runs if r.primary_value is not None]
    if not values:
        return None
    return max(values) if direction == "maximize" else min(values)


@register("experiment.iterate")
@_guarded
async def experiment_iterate(ctx: ActionContext, params: dict[str, Any]) -> dict[str, Any]:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        experiment = await _get_experiment(session, ctx)
        await _set_status(ctx, session, experiment, "running")

        plan: dict[str, Any] = dict(experiment.plan or {})
        pm = plan.get("primary_metric") or {}
        pm_name = str(pm.get("name") or "")
        pm_direction = str(pm.get("direction") or "maximize")
        if not pm_name:
            raise ValueError("实验计划缺少 primary_metric，无法迭代")

        budget = experiment.budget or {}
        max_hours = float(budget.get("max_hours") or 0)
        max_runs = int(budget.get("max_runs") or 0)
        no_improve_stop = int(budget.get("no_improve_stop") or DEFAULT_NO_IMPROVE_STOP)

        state = _iteration_state(experiment)
        files: dict[str, str] = dict(ctx.checkpoint.get("exp_files") or {})

        # 断点安全：迭代起始时间与已完成轮次都可从 DB / checkpoint 恢复
        iterate_cp = dict(ctx.checkpoint.get("iterate") or {})
        if iterate_cp.get("started_at"):
            iterate_started = datetime.fromisoformat(str(iterate_cp["started_at"]))
        else:
            iterate_started = utcnow()
            iterate_cp["started_at"] = iterate_started.isoformat()
            ctx.checkpoint["iterate"] = iterate_cp
            await _persist_checkpoint(session, ctx)

        prior_runs = (
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
        best = _best_primary_value(list(prior_runs), pm_direction)
        next_seq = (prior_runs[-1].seq + 1) if prior_runs else 1
        rounds = 0
        history: list[dict[str, Any]] = [
            {
                "seq": r.seq,
                "status": r.status,
                "exit_code": r.exit_code,
                "primary_value": r.primary_value,
            }
            for r in prior_runs
        ]
        stopped_reason: str | None = state.get("stopped_reason")

        executor = await _open_executor(session, ctx, experiment)
        try:
            while stopped_reason is None:
                # 协作式取消：每轮开始先查 voyage 状态
                if await _voyage_cancelled(session, ctx):
                    await session.refresh(experiment)
                    if experiment.status not in EXPERIMENT_TERMINAL_STATUSES:
                        await _set_status(ctx, session, experiment, "cancelled")
                    return {"cancelled": True, "rounds": rounds}

                seq = next_seq
                if max_runs and seq > max_runs:  # 恢复现场：已跑满就直接收口
                    stopped_reason = "max_runs"
                    break
                if rounds > 0 and max_hours and _elapsed_hours(iterate_started) > max_hours:
                    stopped_reason = "max_hours"
                    break

                # ---- launch + 轮询（复用 _poll_run：cancel/日志镜像/指标/超时 kill） ----
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
                if observation.get("cancelled"):
                    return {"cancelled": True, "rounds": rounds, "seq": seq}
                next_seq = seq + 1
                rounds += 1

                # ---- 可选 workdir/metrics.json 合并（平台确定性解析，非 LLM） ----
                metrics_text = await executor.read_metrics_json()
                if metrics_text:
                    extra_points = parse_metrics_json(metrics_text)
                    if extra_points:
                        run.metrics = merge_metrics(run.metrics, extra_points)
                        experiment.metrics = merge_metrics(experiment.metrics, extra_points)

                # ---- 主指标解析 + direction 感知比较 ----
                primary_value = extract_primary_value(run.metrics, pm_name)
                run.primary_value = primary_value
                if primary_value is not None:
                    if is_improvement(primary_value, best, pm_direction):
                        best = primary_value
                        state["no_improve_streak"] = 0
                    else:
                        state["no_improve_streak"] += 1
                history.append(
                    {
                        "seq": seq,
                        "status": run.status,
                        "exit_code": run.exit_code,
                        "primary_value": primary_value,
                    }
                )
                experiment.iteration_state = dict(state)
                await session.commit()

                # ---- structured reflection（stage=experiment，JSON 校验重试 2） ----
                log_lines, _ = experiments_service.read_local_log_tail(
                    run.log_path, _LOG_TAIL_FOR_REFLECTION
                )
                hyp_count = len(plan.get("hypotheses", []))
                reflection_user = (
                    f"实验计划：{json.dumps(plan, ensure_ascii=False)[:4000]}\n"
                    f"主指标：{json.dumps(pm, ensure_ascii=False)}（假设共 {hyp_count} 条）\n"
                    f"本轮运行：seq={seq} status={run.status} exit_code={run.exit_code} "
                    f"primary_value={primary_value}\n"
                    f"历史各轮：{json.dumps(history, ensure_ascii=False)}\n"
                    f"迭代状态：无提升连续 {state['no_improve_streak']} 轮，"
                    f"debug 已用 {state['debug_count']}/{MAX_DEBUG_FIXES} 次\n"
                    f"本轮日志尾部：\n" + "\n".join(log_lines)
                )
                reflection = await _complete_json(
                    ctx,
                    system=REFLECTION_SYSTEM_PROMPT,
                    user=reflection_user,
                    validate=validate_reflection,
                )
                run.reflection = reflection

                # ---- 假设回写 + iteration_state 持续落库 ----
                plan = _apply_hypothesis_updates(plan, reflection["hypothesis_updates"])
                experiment.plan = plan
                experiment.iteration_state = dict(state)
                await session.commit()

                # ---- decision 分支与终止条件（五项，docs/api-m5-a.md §1） ----
                decision = reflection["decision"]
                hyps = plan.get("hypotheses", [])
                if decision == "stop":
                    stopped_reason = reflection.get("stop_reason") or "decision_stop"
                    break
                if hyps and all(h.get("status") != "testing" for h in hyps):
                    stopped_reason = "hypotheses_resolved"
                    break
                if state["no_improve_streak"] >= no_improve_stop:
                    stopped_reason = "no_improve"
                    break
                if max_runs and seq >= max_runs:
                    stopped_reason = "max_runs"
                    break
                if max_hours and _elapsed_hours(iterate_started) > max_hours:
                    stopped_reason = "max_hours"
                    break
                if decision == "debug":
                    if state["debug_count"] >= MAX_DEBUG_FIXES:
                        stopped_reason = "debug_limit"
                        break
                    state["debug_count"] += 1
                    experiment.iteration_state = dict(state)
                    await session.commit()

                # ---- improve / debug：LLM 改文件（diff 说明进 prompt）→ SSH 覆写 ----
                system_prompt = (
                    DEBUG_SYSTEM_PROMPT if decision == "debug" else IMPROVE_SYSTEM_PROMPT
                )
                fix_user = (
                    f"当前文件：{json.dumps(files, ensure_ascii=False)[:8000]}\n\n"
                    f"reflection 观察：{reflection['observation']}\n"
                    f"诊断：{reflection['diagnosis']}\n"
                    f"planned_change（修改说明）：{reflection.get('planned_change') or '（无）'}\n"
                    f"本轮 exit_code：{run.exit_code}\n"
                    f"本轮日志尾部：\n" + "\n".join(log_lines[-20:])
                )
                files = await _complete_json(
                    ctx, system=system_prompt, user=fix_user, validate=validate_files
                )
                await executor.write_files(files)

                # ---- checkpoint 记轮次进度（断点安全） ----
                ctx.checkpoint["exp_files"] = files
                iterate_cp["last_completed_seq"] = seq
                ctx.checkpoint["iterate"] = iterate_cp
                await _persist_checkpoint(session, ctx)
        finally:
            await executor.close()

        state["stopped_reason"] = stopped_reason
        experiment.iteration_state = dict(state)
        await session.commit()
        iterate_cp["stopped_reason"] = stopped_reason
        ctx.checkpoint["iterate"] = iterate_cp
        await _persist_checkpoint(session, ctx)

    return {
        "rounds": rounds,
        "total_runs": len(history),
        "stopped_reason": stopped_reason,
        "primary_values": [h["primary_value"] for h in history],
        "iteration_state": state,
    }


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


# ---- 5. 图表：metrics_all.json → LLM 绘图脚本 → run_plot → 拉回 → VLM 质检 ----


async def _figure_qc(
    ctx: ActionContext, experiment: Experiment, images: list[bytes]
) -> dict[str, Any]:
    """VLM 质检（stage=experiment 多模态，模式同 figure_annotate）：
    解析失败重试 1 次，仍失败降级为通过（caption 置空，不阻塞管线）。"""
    pm = (experiment.plan or {}).get("primary_metric")
    user_prompt = (
        f"实验主指标：{json.dumps(pm, ensure_ascii=False)}\n"
        f"附带 {len(images)} 张实验图表（index 从 0 开始，与图片顺序一致），请逐张质检并配图注。"
    )
    messages = [
        Message(role="system", content=FIGURE_QC_SYSTEM_PROMPT),
        Message(role="user", content=user_prompt),
    ]
    for _attempt in range(2):
        try:
            result = await ctx.llm.complete(
                "experiment",
                messages,
                images=images,
                user_id=ctx.run.created_by,
                project_id=ctx.run.project_id,
                voyage_id=ctx.run.id,
            )
            data = _extract_json(result.content)
            if not isinstance(data, dict) or not isinstance(data.get("passed"), bool):
                raise ValueError("figure QC payload invalid")
            captions: dict[int, str] = {}
            for item in data.get("figures") or []:
                if isinstance(item, dict) and isinstance(item.get("index"), int):
                    caption = item.get("caption")
                    if caption:
                        captions[int(item["index"])] = str(caption)
            issues = [str(i) for i in (data.get("issues") or [])]
            return {"passed": data["passed"], "captions": captions, "issues": issues}
        except asyncio.CancelledError:
            raise
        except (ValueError, TypeError, KeyError, json.JSONDecodeError):
            continue
    return {"passed": True, "captions": {}, "issues": [], "degraded": True}


async def _pull_figures(
    executor: ssh_exec.SSHExecutor, experiment_id: uuid.UUID, names: list[str]
) -> list[str]:
    """把远端 figures/*.png（及同名 .pdf）拉回本地镜像目录，返回有序 PNG 文件名。

    远端文件名过白名单正则（防 ls 输出注入目录穿越），非法名跳过。
    """
    pngs = sorted(n for n in names if n.endswith(".png") and _FIGURE_NAME_RE.match(n))
    pdfs = {n for n in names if n.endswith(".pdf") and _FIGURE_NAME_RE.match(n)}
    fig_dir = experiments_service.figures_dir(experiment_id)
    fig_dir.mkdir(parents=True, exist_ok=True)
    for png in pngs:
        data = await executor.read_file(f"figures/{png}")
        (fig_dir / png).write_bytes(data)
        pdf = png[: -len(".png")] + ".pdf"
        if pdf in pdfs:
            (fig_dir / pdf).write_bytes(await executor.read_file(f"figures/{pdf}"))
    return pngs


@register("experiment.figures")
@_guarded
async def experiment_figures(ctx: ActionContext, params: dict[str, Any]) -> dict[str, Any]:
    async with get_sessionmaker()() as session:
        experiment = await _get_experiment(session, ctx)
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
        plan = experiment.plan or {}
        # 平台确定性汇总：全部 run 的解析 metrics → workdir/metrics_all.json
        metrics_all = {
            "primary_metric": plan.get("primary_metric"),
            "runs": [
                {
                    "seq": r.seq,
                    "status": r.status,
                    "exit_code": r.exit_code,
                    "primary_value": r.primary_value,
                    "metrics": r.metrics or {},
                }
                for r in runs
            ],
            "experiment_metrics": experiment.metrics or {},
        }
        metrics_all_text = json.dumps(metrics_all, ensure_ascii=False)

        plot_files = ctx.checkpoint.get("plot_files")
        if not isinstance(plot_files, dict):
            plot_files = None
        fixes = 0
        qc_passed = False
        problem: str | None = None
        entries: list[dict[str, Any]] = []

        executor = await _open_executor(session, ctx, experiment)
        try:
            await executor.write_files({"metrics_all.json": metrics_all_text})
            while True:
                if plot_files is None:
                    plot_user = (
                        f"主指标：{json.dumps(plan.get('primary_metric'), ensure_ascii=False)}\n"
                        f"metrics_all.json 内容预览：{metrics_all_text[:4000]}\n"
                        + (f"上一版脚本的问题（请修复）：{problem}" if problem else "")
                    )
                    plot_files = await _complete_json(
                        ctx,
                        system=PLOT_SYSTEM_PROMPT,
                        user=plot_user,
                        validate=validate_plot_files,
                    )
                    ctx.checkpoint["plot_files"] = plot_files
                await executor.write_files(plot_files)

                result = await executor.run_plot()
                if result.exit_status != 0:
                    entries = []
                    problem = (
                        f"脚本执行失败（exit={result.exit_status}）："
                        f"{(result.stderr or result.stdout)[-_STDERR_CHARS:]}"
                    )
                else:
                    names = await executor.list_dir("figures")
                    pngs = await _pull_figures(executor, experiment.id, names)
                    if not pngs:
                        entries = []
                        problem = "脚本执行成功但 figures/ 目录下没有 PNG 输出"
                    else:
                        images: list[bytes] = []
                        sendable: list[str] = []
                        for name in pngs[:MAX_QC_IMAGES]:
                            data = experiments_service.figure_local_path(
                                experiment.id, name
                            ).read_bytes()
                            if len(data) > MAX_IMAGE_BYTES:
                                continue
                            sendable.append(name)
                            images.append(data)
                        qc = (
                            await _figure_qc(ctx, experiment, images)
                            if images
                            else {"passed": True, "captions": {}, "issues": []}
                        )
                        entries = [
                            {
                                "index": i,
                                "name": name,
                                "caption": qc["captions"].get(sendable.index(name))
                                if name in sendable
                                else None,
                                "path": str(
                                    experiments_service.figure_local_path(experiment.id, name)
                                ),
                            }
                            for i, name in enumerate(pngs)
                        ]
                        if qc["passed"]:
                            qc_passed = True
                            break
                        problem = "质检不合格：" + ("；".join(qc["issues"]) or "（未给出原因）")
                if fixes >= MAX_FIGURE_FIXES:
                    break  # 修复次数用尽：带现有产物降级收口（不因绘图阻塞报告）
                fixes += 1
                plot_files = None  # 触发按 problem 重生成脚本
        finally:
            await executor.close()

        experiment.figures = entries
        await session.commit()

    return {
        "figures": len(entries),
        "qc_passed": qc_passed,
        "fixes": fixes,
        **({"problem": problem} if not qc_passed and problem else {}),
    }


# ---- 6. 报告（stage=experiment） ----


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
        runs_brief = [
            {
                "seq": r.seq,
                "status": r.status,
                "exit_code": r.exit_code,
                "primary_value": r.primary_value,
                "decision": (r.reflection or {}).get("decision"),
            }
            for r in runs
        ]
        user_prompt = (
            f"实验计划：{json.dumps(experiment.plan or {}, ensure_ascii=False)[:4000]}\n"
            f"迭代各轮：{json.dumps(runs_brief, ensure_ascii=False)}\n"
            f"迭代状态：{json.dumps(experiment.iteration_state or {}, ensure_ascii=False)}\n"
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
