"""experiment voyage 动作（kind ``experiment``，docs/api-m5-a.md §1 + docs/voyage-loop.md §7）。

启动计划：experiment.plan →（compute_budget 闸门）experiment.setup →
         experiment.smoke → experiment.run（第 1 轮）→ experiment.analyze（第 1 轮）
后续轮次由 analyze 的 plan_signal 走引擎确定性分支表动态追加：
         improve/debug → 下一轮 run + analyze；终止 → experiment.figures → experiment.report

约定：
- LLM 只产出 plan JSON / 代码文件内容 / reflection JSON / 绘图脚本 / 报告 markdown，
  远程命令一律走 services/ssh_exec 的白名单模板（LLM 永远不拼 shell）；
- Experiment.status 与步骤联动（awaiting_gate/setup/running/reporting/done），
  每次流转发 WS ``experiment.status``；
- 步骤均声明 ``on_failure="fail"``：执行异常即 voyage failed，动作内部先把
  Experiment 置 failed 再抛错（_guarded）；轮次的非零退出码**不是**步骤失败——
  observation 携带 exit_code，由 analyze 诊断走 debug 分支；
- experiment.run：单轮 launch → 轮询（30s，协作式 cancel / 日志镜像 /
  POLARIS_METRIC + 可选 metrics.json 解析 / 预算超时）→ 主指标 direction 感知比较；
- experiment.analyze：LLM structured reflection → 假设回写 → 终止判定
  （stop/假设定论/无提升/max_runs/max_hours/debug 限额）→ improve/debug 改代码
  → plan_signal（continue/finish）；iteration_state 持续落库；
- experiment.figures：平台写 metrics_all.json → LLM 绘图脚本（只准读该文件）→
  白名单 run_plot → 拉回 figures/*.png(+.pdf) → VLM 质检（失败修脚本 ≤2 次）。
"""

import asyncio
import contextlib
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
from app.services.figure_annotate import prepare_image_for_llm

RUN_POLL_SECONDS = 30.0  # 正式运行轮询间隔（测试 monkeypatch 为 0）
MAX_SMOKE_FIXES = 2  # 冒烟失败回 LLM 修代码的次数上限
MAX_DEBUG_FIXES = 3  # 迭代内 debug 分支独立限额（docs/api-m5-a.md §1）
MAX_FIGURE_FIXES = 2  # 绘图脚本执行失败 / VLM 质检不合格的修复次数上限
DEFAULT_NO_IMPROVE_STOP = 2  # 连续 N 轮主指标无提升即停（budget.no_improve_stop 可覆盖）
MAX_QC_IMAGES = 8  # 单次质检最多送 LLM 的图数
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
 "conditions": [{"name": "baseline", "role": "baseline", "description": "对照组"},
                {"name": "treatment_a", "role": "treatment", "description": "处理组"}],
 "eval_protocol": {"dataset": "数据集/来源", "split": "评测划分", "metric": "评测指标",
                   "n_examples": 100, "n_samples": 1},
 "datasets": [{"name": "HF数据集名或来源", "purpose": "test|corpus|train", "size_hint": "规模"}],
 "budget_estimate": {"gpu_hours": 2, "runs": 3}}
约束：
- hypotheses 1-5 条且必须可被实验证实/证伪；steps 3-8 条；
- primary_metric 必填：name 是评测代码 POLARIS_METRIC 输出的指标名（对照实验里应是主处理组或均值），
  direction 只能取 maximize / minimize；budget_estimate 是对象（至少含 gpu_hours）；
- **对照实验（复现论文常见）**：若研究方案对比多个方法/配置（如 baseline vs 改进），
  必须在 conditions 里列出（恰一个 role=baseline，其余 role=treatment），并把评测协议写进
  eval_protocol（数据集/划分/指标/样本数）、把要用的真实数据集写进 datasets；
  代码将对每个 condition 用同一评测集跑并逐条 POLARIS_METRIC 输出，供平台做对照分析。
- 若是单一配置的调参类实验，conditions/eval_protocol/datasets 可省略。
"""

CODE_SYSTEM_PROMPT = """\
你是 Experiment Lab 的实验工程师，为给定实验计划编写可直接运行的代码文件。
只输出一个 JSON 对象，不要输出任何其他文字或 Markdown 代码块，格式：
{"files": {"requirements.txt": "内容", "run.sh": "内容", "train.py": "内容"}}
硬约束：
- 必须包含 requirements.txt 与 run.sh；文件路径必须是相对路径（禁止 .. / 绝对路径 / ~）
- run.sh 必须支持 --smoke 参数：只跑极小样本（如几条数据、1 个模型、去掉耗时条件）快速验证
  代码可跑通；非 smoke 时跑计划里的真实规模。用 .venv/bin/python 运行
- 评测/训练代码必须用 print('POLARIS_METRIC ' + json.dumps({"name": 指标名, "step": 步数, \
"value": 数值})) 输出关键指标；数字必须来自真实计算，严禁硬编码任何结果
- 数据只读写工作目录之内（可在 workdir 下建 data_cache/ 缓存）；不得读写 workdir 之外的路径
- **数据集**：评测/复现类实验可以用 HuggingFace `datasets` 下载真实公开数据集
  （平台已注入 HF 镜像与出网代理，正常 load_dataset 即可），下载到 workdir 内缓存；
  合成数据仅用于 smoke。规模按计划的 eval_protocol/datasets 控制，避免超大下载
- **对照实验**：若计划给了 conditions（baseline + treatments），代码必须对每个 condition
  用同一评测集、同一协议评测，并对每个 condition 单独输出 POLARIS_METRIC（指标名带上
  condition 与模型，如 "accuracy/<model>/<condition>"），使平台能对照 baseline vs treatment；
  eval_protocol 里的数据集/划分/指标/样本数要如实落实
"""

FIX_SYSTEM_PROMPT = (
    CODE_SYSTEM_PROMPT
    + """\

现在冒烟测试失败了，请根据报错修复代码：输出修复后的完整文件集合（同上 JSON 格式）。
"""
)

# 自动迭代优化：proposer 能读到**全部历史尝试**的源码/得分/执行轨迹（不是压缩后的反馈），据此提出
# 下一次尝试。通用机制，适用于调参/提示优化/特征/算法/流程等任何「改实现以提升指标」的实验；灵感来自
# 「richer access to prior experience 优于过度压缩反馈」这一点，非某类实验专属。
IMPROVE_SYSTEM_PROMPT = (
    CODE_SYSTEM_PROMPT
    + """\

现在进入自动迭代优化：目标是改进实验代码/配置，让主指标更好。下面给你**全部**历史尝试的源码、得分与
执行轨迹（不是压缩后的反馈）——请综合所有先验经验，不要只盯着最后一轮：
- 借鉴高分尝试里有效的做法，避开低分尝试已被证伪的思路；说明你这次改动的假设与依据。
- 提出一个**有依据的新尝试**（视实验而定，可改：算法/超参/数据处理/提示词/特征/检索/流程等），
  而不是无谓微调；有把握时可较大重构，也可延续 reflection 的改进方向。
- **只改被优化的实现，不改评测协议/数据集/主指标口径**（评测本身保持不变，确保各次尝试可比）。
输出修改后的完整文件集合（同上 JSON 格式）。
"""
)

DEBUG_SYSTEM_PROMPT = (
    CODE_SYSTEM_PROMPT
    + """\

现在自动迭代中的正式运行失败了。先**诊断失败类别与根因**，再决定怎么修——不要只盯着「改几行代码」，
可以在文件集合内做**方案级调整**：
- 依赖/环境（缺包、版本冲突、CUDA/显存不足、装不上）→ 改 requirements.txt / run.sh：换/装依赖、
  选设备、降 batch、精简依赖、必要时换实现方式绕开装不上的包。
- 模型/框架不兼容（架构不被支持、多模态模型用于纯文本、tokenizer 不匹配、加载报错）→ 换用兼容的
  加载方式/框架/模型规格（在你能控制的文件范围内）。
- 配置（超时、样本过大、路径错、显存 OOM）→ 调小规模、修正路径、减小 batch/长度。
- 代码 bug → 修对应逻辑。
先用一句话点明诊断（失败属于上面哪类、根因是什么），再输出修复后的**完整文件集合**
（同上 JSON 格式）。只在文件集合里改，别动评测协议/数据集/主指标口径（保证可比）。
"""
)

def _render_attempt_archive(
    archive: list[dict[str, Any]], per_file_cap: int = 2000, best_file_cap: int = 4000
) -> str:
    """把历史尝试（源码+得分+轨迹）渲染进迭代 proposer 提示——通用的「先验经验档案」。

    非某类实验专属：渲染每次尝试的**全部**源码文件（不假设入口文件名），最优尝试给更长上下文，
    其余截断；轨迹给尾部。让 proposer 据全量历史而非最后一轮提出下一次尝试。"""
    if not archive:
        return ""

    def _score(c: dict[str, Any]) -> tuple[int, float]:
        v = c.get("primary_value")
        return (1, float(v)) if isinstance(v, int | float) else (0, float("-inf"))

    best = max(archive, key=_score)
    parts = [f"历史尝试档案（共 {len(archive)} 次，含源码/得分/轨迹，据此提出下一次尝试）："]
    for c in archive:
        star = " ★迄今最好" if c is best else ""
        delta = c.get("conditions_delta")
        delta_s = json.dumps(delta, ensure_ascii=False) if delta else "—"
        parts.append(
            f"\n[尝试 seq={c.get('seq')} | 主指标={c.get('primary_value')}{star} | 对照={delta_s}]"
        )
        cap = best_file_cap if c is best else per_file_cap
        # 渲染全部源码文件（跳过 requirements 这类噪音），不假设固定入口名，保证通用
        for name, code in sorted((c.get("files") or {}).items()):
            if not code or name == "requirements.txt":
                continue
            parts.append(f"源码（{name}，截断 {cap}）：\n{str(code)[:cap]}")
        trace = c.get("trace") or ""
        if trace:
            parts.append(f"执行轨迹尾部：{trace[-600:]}")
    return "\n".join(parts) + "\n"


# ---- 按实验 params 条件追加的 system prompt 段落（plan 与全部 codegen prompt 共用） ----

EVAL_MODEL_PROMPT_SECTION = """\

评测模型（LLM API 访问）：
- 平台已在工作目录写入 llm_config.json，内容为 {"base_url": ..., "api_key": ..., "model": ...}；
  代码必须从该文件读取 LLM 配置（禁止在代码中硬编码任何 api_key），
  用 OpenAI 兼容的 /chat/completions 接口调用该模型；
- 该模型可能是思考型模型（响应中可能带 reasoning_content 思考过程），
  务必设置 max_tokens≥2048，并只读取 choices[0].message.content 作为答案；
- API 有限流：请求失败/超时要做重试（如指数退避），不要因单次失败中断整个评测。
"""

HF_MIRROR_PROMPT_SECTION = """\

HuggingFace 镜像：环境变量 HF_ENDPOINT 已指向 https://hf-mirror.com（平台在 env.sh 注入），
transformers / datasets 按正常方式加载模型与数据集即可，代码里无需再做任何镜像设置。
"""

EXTRA_NOTES_PROMPT_SECTION = """\

用户对本实验的补充说明（务必遵循）：
{notes}
"""

HF_MIRROR_ENDPOINT = "https://hf-mirror.com"


def _prompt_with_context(base: str, ctx: ActionContext) -> str:
    """按 params.eval_model / hf_mirror / extra_notes 给 system prompt 条件追加段落。"""
    params = _params(ctx)
    parts = [base]
    if str(params.get("eval_model") or "").strip():
        parts.append(EVAL_MODEL_PROMPT_SECTION)
    if params.get("hf_mirror"):
        parts.append(HF_MIRROR_PROMPT_SECTION)
    notes = str(params.get("extra_notes") or "").strip()
    if notes:
        parts.append(EXTRA_NOTES_PROMPT_SECTION.format(notes=notes))
    return "".join(parts)


def _platform_env_files(
    ctx: ActionContext, *, proxy_url: str | None = None, no_proxy_extra: str = ""
) -> dict[str, str]:
    """平台生成的 env.sh（固定内容，非 LLM 产物）：恒定导出 POLARIS_WORKDIR，
    hf_mirror 时追加 HF_ENDPOINT 镜像；服务器配置了出网代理时导出 http(s)_proxy，
    并把内网 LLM 地址列入 no_proxy（评测 API 不走代理）。模板执行前会 source。"""
    lines = ["export POLARIS_WORKDIR=$(pwd)"]
    if _params(ctx).get("hf_mirror"):
        lines.append(f"export HF_ENDPOINT={HF_MIRROR_ENDPOINT}")
    if proxy_url:
        no_proxy = "localhost,127.0.0.1"
        if _params(ctx).get("hf_mirror"):
            # 国内镜像直连（走外网代理反而不通，2026-07-15 实测 transformers 连不上）
            no_proxy += ",hf-mirror.com"
        if no_proxy_extra:
            no_proxy += f",{no_proxy_extra}"
        lines.append(f"export http_proxy={proxy_url} https_proxy={proxy_url}")
        lines.append(f"export HTTP_PROXY={proxy_url} HTTPS_PROXY={proxy_url}")
        lines.append(f"export no_proxy={no_proxy} NO_PROXY={no_proxy}")
    return {"env.sh": "\n".join(lines) + "\n"}


async def _eval_model_config_file(ctx: ActionContext) -> dict[str, str]:
    """eval_model 非空时：从 LLM 路由 default stage 解析 provider（api_key 已解密），
    生成 llm_config.json 内容。审计侧安全：write_files 的审计只记路径与字节数，
    api_key 不会出现在任何日志/Activity。"""
    eval_model = str(_params(ctx).get("eval_model") or "").strip()
    if not eval_model:
        return {}
    _provider, route = await ctx.llm.resolve("default")
    config = {
        "base_url": route.base_url or "",
        "api_key": route.api_key,
        "model": eval_model,
    }
    return {"llm_config.json": json.dumps(config, ensure_ascii=False, indent=2) + "\n"}


REFLECTION_SYSTEM_PROMPT = """\
你是 Experiment Lab 的实验分析师，基于本轮运行结果做结构化反思并决定下一步。
只输出一个 JSON 对象，不要输出任何其他文字或 Markdown 代码块，格式：
{"observation": "本轮结果观察", "diagnosis": "原因诊断",
 "hypothesis_updates": [{"index": 0, "status": "verified|falsified|testing", "evidence": "证据"}],
 "decision": "improve|debug|stop", "planned_change": "下一轮计划修改", "stop_reason": null}
约束：
- hypothesis_updates 的 index 是假设清单下标（从 0 开始），status 只能取 verified/falsified/testing
- 本轮运行失败（exit_code 非 0）时 decision 用 debug；结果已足以回答全部假设时用 stop
- 本轮失败时，diagnosis 要点明**失败类别**（依赖/环境、模型或框架不兼容、配置/超时/OOM、代码 bug）
  与根因，并在 planned_change 里给出方案级修法（可换依赖/框架/加载方式，不限于改几行代码）
- decision=stop 时 stop_reason 必填一句话；decision=improve 时 planned_change 必填
- 对照实验：若给了「对照汇总」，据 baseline vs treatment 的 delta 判断假设成立与否
  （处理组是否优于 baseline），别只看单个 primary_value；对照结果已清晰时可直接 stop
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
若给了「对照汇总」（对照实验）：用一个 markdown 表格列出各 condition（含 baseline）的指标与
相对 baseline 的 delta，并据此判断处理组是否显著优于 baseline、结论是否复现了预期效应。
数字一律引用给定的指标数据/对照汇总，不得编造。
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
    out: dict[str, Any] = {
        "hypotheses": hypotheses,
        "repro_strategy": repro.strip(),
        "steps": steps,
        "primary_metric": {"name": pm_name.strip(), "direction": pm_direction},
        "budget_estimate": budget,
    }
    # 对照实验的可选结构（复现类实验用）：conditions/eval_protocol/datasets 透传，供 setup
    # 代码生成与 analyze/report 对照分析消费。恰一个 baseline 才算有效对照。
    conditions = data.get("conditions")
    if isinstance(conditions, list) and conditions:
        norm = []
        for c in conditions:
            if not isinstance(c, dict) or not str(c.get("name") or "").strip():
                continue
            role = c.get("role") if c.get("role") in ("baseline", "treatment") else "treatment"
            norm.append(
                {
                    "name": str(c["name"]).strip(),
                    "role": role,
                    "description": str(c.get("description") or "").strip(),
                }
            )
        if norm:
            out["conditions"] = norm
    if isinstance(data.get("eval_protocol"), dict):
        out["eval_protocol"] = data["eval_protocol"]
    if isinstance(data.get("datasets"), list):
        out["datasets"] = data["datasets"]
    return out


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


def _last_value(series: Any) -> float | None:
    """从一条 metric 序列取末值（兼容 [{step,value}] 列表或标量）。"""
    if isinstance(series, list) and series:
        last = series[-1]
        v = last.get("value") if isinstance(last, dict) else last
    else:
        v = series
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _conditions_delta(experiment: Experiment) -> dict[str, Any] | None:
    """对照实验的确定性汇总：按 plan.conditions 把 experiment.metrics 里各指标末值归到
    对应 condition（指标名以 /<condition> 结尾即归属，只聚合主指标族），算每组均值与相对
    baseline 的 delta。无 conditions 或无可归属指标时返回 None（退化为原单指标分析）。"""
    plan = experiment.plan or {}
    conditions = plan.get("conditions")
    if not isinstance(conditions, list) or not conditions:
        return None
    pm_name = str((plan.get("primary_metric") or {}).get("name") or "").strip()
    pm_root = pm_name.split("/")[0] if pm_name else ""
    lasts = {name: _last_value(s) for name, s in (experiment.metrics or {}).items()}

    def _belongs(name: str, cond: str) -> bool:
        if not (name.endswith(f"/{cond}") or name == cond):
            return False
        return not pm_root or name.split("/")[0] == pm_root or name == cond

    scores: dict[str, float] = {}
    for c in conditions:
        cond = str(c.get("name") or "").strip()
        if not cond:
            continue
        vals = [v for name, v in lasts.items() if v is not None and _belongs(name, cond)]
        if vals:
            scores[cond] = round(sum(vals) / len(vals), 3)
    if not scores:
        return None
    baseline = next(
        (
            str(c.get("name")).strip()
            for c in conditions
            if c.get("role") == "baseline" and str(c.get("name")).strip() in scores
        ),
        None,
    )
    deltas: dict[str, float] = {}
    if baseline is not None:
        deltas = {c: round(v - scores[baseline], 3) for c, v in scores.items() if c != baseline}
    return {"baseline": baseline, "scores": scores, "deltas_vs_baseline": deltas}


def _proposal_context(idea: Idea) -> str:
    """把 idea 2.0 深耕产物（Research Proposal）的结构化研究方案渲染成计划提示上下文。

    深耕 idea（depth=proposal）的 goal 带 objectives/success_criteria/resources_needed 与专为
    生成实验设计的 smoke_plan（baselines/datasets/metrics/conditions）——把「研究方案」忠实转成
    「实验计划」的关键输入；sketch 草案回退空串。"""
    if idea.depth != "proposal" or not isinstance(idea.goal, dict):
        return ""
    g = idea.goal
    parts = ["\n研究方案（Research Proposal，务必据此产出忠实的实验计划）："]
    if idea.research_type:
        parts.append(f"- 研究类型：{idea.research_type}")
    for key, label in (("task", "任务"), ("question", "研究问题"), ("scope", "范围")):
        if g.get(key):
            parts.append(f"- {label}：{str(g[key])[:400]}")
    for key, label in (("objectives", "研究目标"), ("success_criteria", "成功标准")):
        vals = g.get(key)
        if isinstance(vals, list) and vals:
            parts.append(f"- {label}：" + "；".join(str(v)[:120] for v in vals[:6]))
    res = g.get("resources_needed")
    if isinstance(res, dict) and res.get("data"):
        d = res["data"]
        rendered = "；".join(str(v)[:100] for v in d[:5]) if isinstance(d, list) else str(d)[:300]
        parts.append(f"- 需要的数据：{rendered}")
    exp_design = g.get("smoke_plan") or g.get("experiments")
    if exp_design:
        design_json = json.dumps(exp_design, ensure_ascii=False)[:1500]
        parts.append(f"- 论文/方案给出的实验设计：{design_json}")
    if isinstance(idea.evidence, list) and idea.evidence:
        grounds = [
            str(e.get("title") or e.get("why") or "")[:80]
            for e in idea.evidence
            if isinstance(e, dict)
        ][:4]
        if any(grounds):
            parts.append("- 依据文献：" + "；".join(x for x in grounds if x))
    return "\n".join(parts) + "\n"


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
                f"想法详情：\n{(idea.content or '')[:4000]}\n"
                f"{_proposal_context(idea)}\n"
                f"相关 wiki 摘要：\n{wiki_context}\n\n"
                f"预算约束：{json.dumps(experiment.budget or {}, ensure_ascii=False)}\n"
                f"GPU 提示：{gpu_hint or '（无）'}"
            )
            plan = await _complete_json(
                ctx,
                system=_prompt_with_context(PLAN_SYSTEM_PROMPT, ctx),
                user=user_prompt,
                validate=validate_plan,
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
                f"实验计划：{json.dumps(experiment.plan or {}, ensure_ascii=False)[:8000]}\n"
                f"预算：{json.dumps(experiment.budget or {}, ensure_ascii=False)}"
            )
            files = await _complete_json(
                ctx,
                system=_prompt_with_context(CODE_SYSTEM_PROMPT, ctx),
                user=user_prompt,
                validate=validate_files,
            )
            ctx.checkpoint["exp_files"] = files

        # 平台注入文件（非 LLM 产物，不进 exp_files，避免被 smoke/iterate 修复覆写）：
        # env.sh（POLARIS_WORKDIR/HF_ENDPOINT/代理）与可选 llm_config.json（评测模型）
        eval_files = await _eval_model_config_file(ctx)
        llm_host = ""
        if eval_files:
            from urllib.parse import urlparse

            llm_host = (
                urlparse(json.loads(eval_files["llm_config.json"])["base_url"]).hostname or ""
            )

        executor = await _open_executor(session, ctx, experiment)
        platform_files = (
            _platform_env_files(ctx, proxy_url=executor.proxy_url, no_proxy_extra=llm_host)
            | eval_files
        )
        try:
            await executor.mkdir_workdir()
            experiment.workdir = executor.workdir
            experiment.server_host = executor.host
            await session.commit()
            written = await executor.write_files(files)
            written += await executor.write_files(platform_files)
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
                    ctx,
                    system=_prompt_with_context(FIX_SYSTEM_PROMPT, ctx),
                    user=user_prompt,
                    validate=validate_files,
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


@register("experiment.run")
@_guarded
async def experiment_run(ctx: ActionContext, params: dict[str, Any]) -> dict[str, Any]:
    """单轮正式运行：launch → 轮询（cancel/日志镜像/指标/超时 kill）→ metrics 合并。

    原 experiment.iterate 的一轮循环体（docs/voyage-loop.md §7）：每轮是独立的
    任务步骤，可见、可审计、可断点恢复；非零退出码不算步骤失败（observation 携带
    exit_code，交由 experiment.analyze 诊断走 debug 分支）。
    """
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

        # 迭代起始时间（多轮共享；断点/恢复从 checkpoint 读回）
        iterate_cp = dict(ctx.checkpoint.get("iterate") or {})
        if iterate_cp.get("started_at"):
            iterate_started = datetime.fromisoformat(str(iterate_cp["started_at"]))
        else:
            iterate_started = utcnow()
            iterate_cp["started_at"] = iterate_started.isoformat()
            ctx.checkpoint["iterate"] = iterate_cp

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
        seq = (prior_runs[-1].seq + 1) if prior_runs else 1

        # 恢复现场护栏：预算已满就不再启动（正常路径由 analyze 的终止判定拦截）
        for reason, exhausted in (
            ("max_runs", bool(max_runs and seq > max_runs)),
            (
                "max_hours",
                bool(prior_runs and max_hours and _elapsed_hours(iterate_started) > max_hours),
            ),
        ):
            if exhausted:
                iterate_cp["stopped_reason"] = reason
                ctx.checkpoint["iterate"] = iterate_cp
                return {
                    "skipped": True,
                    "stopped_reason": reason,
                    "plan_signal": {"decision": "finish", "stopped_reason": reason},
                }

        best = _best_primary_value(list(prior_runs), pm_direction)
        state = _iteration_state(experiment)

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
            # _poll_run 可能因断连重连返回新的 executor，后续读取/关闭都用返回的这个
            observation, executor = await _poll_run(
                ctx, session, executor, experiment, run, max_hours
            )
            if observation.get("cancelled"):
                return observation  # _poll_run 已 kill 进程并同步实验状态

            # 可选 workdir/metrics.json 合并（平台确定性解析，非 LLM）
            metrics_text = await executor.read_metrics_json()
            if metrics_text:
                extra_points = parse_metrics_json(metrics_text)
                if extra_points:
                    run.metrics = merge_metrics(run.metrics, extra_points)
                    experiment.metrics = merge_metrics(experiment.metrics, extra_points)
        finally:
            await executor.close()

        # 主指标解析 + direction 感知比较（无提升连击数供 analyze 终止判定用）
        primary_value = extract_primary_value(run.metrics, pm_name)
        run.primary_value = primary_value
        if primary_value is not None:
            if is_improvement(primary_value, best, pm_direction):
                state["no_improve_streak"] = 0
            else:
                state["no_improve_streak"] += 1
        experiment.iteration_state = dict(state)
        await session.commit()

        # 轮询之外的取消窗口（如 metrics 读取期间被取消）：同步实验状态后安静收尾
        if await _voyage_cancelled(session, ctx):
            await session.refresh(experiment)
            if experiment.status not in EXPERIMENT_TERMINAL_STATUSES:
                await _set_status(ctx, session, experiment, "cancelled")
            return {"cancelled": True, "seq": seq, "run_id": str(run.id)}

        return {**observation, "primary_value": primary_value}


@register("experiment.analyze")
@_guarded
async def experiment_analyze(ctx: ActionContext, params: dict[str, Any]) -> dict[str, Any]:
    """单轮分析：structured reflection → 假设回写 → 终止判定 → improve/debug 改代码。

    产出 plan_signal 供引擎的确定性分支表消费（docs/voyage-loop.md §7）：
    - continue：已按 reflection 改完代码，追加下一轮 run + analyze；
    - finish：终止条件命中（stop/假设定论/无提升/预算/debug 限额），进入收尾。
    终止判定顺序与原 experiment.iterate 完全一致。
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        experiment = await _get_experiment(session, ctx)

        plan: dict[str, Any] = dict(experiment.plan or {})
        pm = plan.get("primary_metric") or {}
        budget = experiment.budget or {}
        max_hours = float(budget.get("max_hours") or 0)
        max_runs = int(budget.get("max_runs") or 0)
        no_improve_stop = int(budget.get("no_improve_stop") or DEFAULT_NO_IMPROVE_STOP)
        state = _iteration_state(experiment)

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
        if not runs:
            raise ValueError("没有可分析的运行轮次")
        run = runs[-1]
        history = [
            {
                "seq": r.seq,
                "status": r.status,
                "exit_code": r.exit_code,
                "primary_value": r.primary_value,
            }
            for r in runs
        ]

        # structured reflection（stage=experiment，JSON 校验重试 2）
        log_lines, _ = experiments_service.read_local_log_tail(
            run.log_path, _LOG_TAIL_FOR_REFLECTION
        )
        hyp_count = len(plan.get("hypotheses", []))
        cond_delta = _conditions_delta(experiment)
        cond_line = (
            f"对照汇总（baseline vs treatment，平台确定性计算）："
            f"{json.dumps(cond_delta, ensure_ascii=False)}\n"
            if cond_delta
            else ""
        )
        run_lasts = {k: _last_value(v) for k, v in (run.metrics or {}).items()}
        reflection_user = (
            f"实验计划：{json.dumps(plan, ensure_ascii=False)[:4000]}\n"
            f"主指标：{json.dumps(pm, ensure_ascii=False)}（假设共 {hyp_count} 条）\n"
            f"本轮运行：seq={run.seq} status={run.status} exit_code={run.exit_code} "
            f"primary_value={run.primary_value}\n"
            f"本轮各指标末值：{json.dumps(run_lasts, ensure_ascii=False)[:1500]}\n"
            f"{cond_line}"
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

        # 尝试存档（通用先验经验档案）：把本轮实现的源码/得分/轨迹存起来，供后续迭代 proposer 读取
        # 全量历史（不是只看上一轮）。记录产生本轮 run 的实现（当前 exp_files）。
        archive = list(ctx.checkpoint.get("attempt_archive") or [])
        archive.append(
            {
                "seq": run.seq,
                "primary_value": run.primary_value,
                "conditions_delta": cond_delta,
                "files": dict(ctx.checkpoint.get("exp_files") or {}),
                "trace": "\n".join(log_lines[-30:]),
                "observation": reflection.get("observation"),
            }
        )
        ctx.checkpoint["attempt_archive"] = archive

        # 假设回写 + iteration_state 落库
        plan = _apply_hypothesis_updates(plan, reflection["hypothesis_updates"])
        experiment.plan = plan
        experiment.iteration_state = dict(state)
        await session.commit()

        # decision 分支与终止条件（顺序与原 iterate 一致，docs/api-m5-a.md §1）
        decision = reflection["decision"]
        hyps = plan.get("hypotheses", [])
        iterate_cp = dict(ctx.checkpoint.get("iterate") or {})
        iterate_started = (
            datetime.fromisoformat(str(iterate_cp["started_at"]))
            if iterate_cp.get("started_at")
            else utcnow()
        )
        stopped_reason: str | None = None
        if decision == "stop":
            stopped_reason = reflection.get("stop_reason") or "decision_stop"
        elif hyps and all(h.get("status") != "testing" for h in hyps):
            stopped_reason = "hypotheses_resolved"
        elif state["no_improve_streak"] >= no_improve_stop:
            stopped_reason = "no_improve"
        elif max_runs and run.seq >= max_runs:
            stopped_reason = "max_runs"
        elif max_hours and _elapsed_hours(iterate_started) > max_hours:
            stopped_reason = "max_hours"
        elif decision == "debug" and state["debug_count"] >= MAX_DEBUG_FIXES:
            stopped_reason = "debug_limit"

        if stopped_reason:
            state["stopped_reason"] = stopped_reason
            experiment.iteration_state = dict(state)
            await session.commit()
            iterate_cp["stopped_reason"] = stopped_reason
            iterate_cp["last_completed_seq"] = run.seq
            ctx.checkpoint["iterate"] = iterate_cp
            return {
                "seq": run.seq,
                "decision": decision,
                "rounds": len(runs),
                "stopped_reason": stopped_reason,
                "plan_signal": {"decision": "finish", "stopped_reason": stopped_reason},
            }

        if decision == "debug":
            state["debug_count"] += 1
            experiment.iteration_state = dict(state)
            await session.commit()

        # improve → 迭代优化 proposer（读全量尝试档案提下一候选）；debug → 按报错修当前文件
        files: dict[str, str] = dict(ctx.checkpoint.get("exp_files") or {})
        if decision == "debug":
            system_prompt = _prompt_with_context(DEBUG_SYSTEM_PROMPT, ctx)
            # 失败诊断也带上「历史尝试档案」：让 debug 能看见前面试过什么、哪些方案已被证伪，
            # 从而做方案级调整（换依赖/框架/加载方式）而非反复在同一条死路上改代码。
            prior = archive[:-1]  # 除当前失败轮外的历史尝试
            archive_ctx = _render_attempt_archive(prior) if prior else ""
            fix_user = (
                (archive_ctx + "\n" if archive_ctx else "")
                + f"当前文件：{json.dumps(files, ensure_ascii=False)[:8000]}\n\n"
                + f"reflection 观察：{reflection['observation']}\n"
                + f"诊断：{reflection['diagnosis']}\n"
                + f"planned_change（修改说明）：{reflection.get('planned_change') or '（无）'}\n"
                + f"本轮 exit_code：{run.exit_code}\n"
                + "本轮日志尾部（据此定位失败类别与根因）：\n"
                + "\n".join(log_lines[-40:])
            )
        else:
            system_prompt = _prompt_with_context(IMPROVE_SYSTEM_PROMPT, ctx)
            fix_user = (
                _render_attempt_archive(archive)
                + f"\n主指标：{json.dumps(pm, ensure_ascii=False)}\n"
                + f"当前尝试 seq={run.seq} 主指标={run.primary_value}；"
                + f"reflection 诊断：{reflection['diagnosis']}\n"
                + f"reflection 改进方向（参考）：{reflection.get('planned_change') or '（无）'}\n"
                + "请综合以上全部尝试的源码/得分/轨迹，提出一个有依据的新尝试，"
                + "输出修改后的完整文件集合。"
            )
        files = await _complete_json(
            ctx, system=system_prompt, user=fix_user, validate=validate_files
        )
        executor = await _open_executor(session, ctx, experiment)
        try:
            await executor.write_files(files)
        finally:
            await executor.close()
        ctx.checkpoint["exp_files"] = files
        iterate_cp["last_completed_seq"] = run.seq
        ctx.checkpoint["iterate"] = iterate_cp

        return {
            "seq": run.seq,
            "decision": decision,
            "rounds": len(runs),
            "plan_signal": {"decision": "continue", "next_round": run.seq + 1},
        }


def _reconnect_backoff(streak: int) -> float:
    """轮询断连后的指数退避秒数（上限 30s）。抽成函数便于测试注入零退避。"""
    return min(30.0, 2.0**streak)


async def _poll_run(
    ctx: ActionContext,
    session: AsyncSession,
    executor: ssh_exec.SSHExecutor,
    experiment: Experiment,
    run: ExperimentRun,
    max_hours: float,
) -> tuple[dict[str, Any], ssh_exec.SSHExecutor]:
    """轮询远端运行直到结束。返回 (observation, executor)——executor 可能在轮询中因
    连接断开而重连，调用方须使用返回的（存活）executor 做后续读取与关闭。

    容错要点：轮询期间底层 SSH 连接可能被服务器 idle 断开或网络抖动切断。远端运行状态
    （run.exit/run.log/pid）都持久化在服务器上，且进程经 nohup 脱离会话——因此瞬时断连
    应「重连后继续跟踪」而非让实验失败（历史 bug：一次 ChannelOpenError 即判实验 failed，
    而进程其实还在跑）。仅在连续多次重连失败后才放弃。"""
    offset = 0
    conn_fail_streak = 0
    max_conn_fails = 6  # 连续重连失败上限（配合指数退避≈数分钟）后才判失败

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
        try:
            await ingest_chunk()  # 收尾：抓最后一段日志
        except Exception as e:  # noqa: BLE001 — 收尾抓日志断连不该翻盘
            if not ssh_exec.is_connection_error(e):
                raise
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

    async def reconnect() -> None:
        nonlocal executor
        with contextlib.suppress(Exception):  # 旧连接已坏，关闭失败无所谓
            await executor.close()
        executor = await _open_executor(session, ctx, experiment)

    while True:
        # 协作式取消：每轮查 voyage 状态（仅 DB，不碰 SSH）
        voyage_status = (
            await session.execute(select(VoyageRun.status).where(VoyageRun.id == ctx.run.id))
        ).scalar_one()
        if voyage_status == "cancelled":
            try:
                await executor.kill_pid(int(run.pid or 0))
                await ingest_chunk()
            except Exception as e:  # noqa: BLE001 — 取消收尾尽力而为
                if not ssh_exec.is_connection_error(e):
                    raise
            run.status = "failed"
            run.finished_at = utcnow()
            await session.commit()
            await session.refresh(experiment)
            if experiment.status not in EXPERIMENT_TERMINAL_STATUSES:
                await _set_status(ctx, session, experiment, "cancelled")
            return {"cancelled": True, "run_id": str(run.id), "seq": run.seq}, executor

        try:
            await ingest_chunk()
            exit_code = await executor.read_exit_code()
            if exit_code is not None:
                return await finish(exit_code), executor
            alive = await executor.check_pid(int(run.pid or 0))
            if not alive:
                # 进程没了但还没读到退出码：再读一次（竞态），仍无则按 failed 收尾
                return await finish(await executor.read_exit_code()), executor
            conn_fail_streak = 0
        except Exception as e:  # noqa: BLE001 — 瞬时断连：重连续跑；其它异常照常抛
            if not ssh_exec.is_connection_error(e):
                raise
            conn_fail_streak += 1
            if conn_fail_streak > max_conn_fails:
                raise RuntimeError(
                    f"SSH 连接反复断开（连续 {conn_fail_streak} 次），放弃轮询 run={run.seq}：{e}"
                ) from e
            session.add(
                Activity(
                    project_id=ctx.run.project_id,
                    actor="system:voyage",
                    kind="experiment.ssh_reconnect",
                    message=f"轮询期间 SSH 断开，重连中（第 {conn_fail_streak} 次）：{type(e).__name__}",  # noqa: E501
                    payload={
                        "experiment_id": str(experiment.id),
                        "run_seq": run.seq,
                        "attempt": conn_fail_streak,
                    },
                )
            )
            await session.commit()
            await asyncio.sleep(_reconnect_backoff(conn_fail_streak))
            try:
                await reconnect()
            except Exception as re:  # noqa: BLE001 — 重连本身失败：下轮继续退避重试
                if not ssh_exec.is_connection_error(re):
                    raise
            continue  # 远端状态持久化，重连后下一轮继续跟踪

        if max_hours >= 0 and _elapsed_hours(run.started_at) > max_hours:
            try:
                await executor.kill_pid(int(run.pid or 0))
                await ingest_chunk()
            except Exception as e:  # noqa: BLE001
                if not ssh_exec.is_connection_error(e):
                    raise
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
                            data = prepare_image_for_llm(
                                experiments_service.figure_local_path(
                                    experiment.id, name
                                ).read_bytes()
                            )
                            if data is None:
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
        cond_delta = _conditions_delta(experiment)
        cond_line = (
            f"对照汇总（baseline vs treatment，平台确定性计算）："
            f"{json.dumps(cond_delta, ensure_ascii=False)}\n"
            if cond_delta
            else ""
        )
        user_prompt = (
            f"实验计划：{json.dumps(experiment.plan or {}, ensure_ascii=False)[:4000]}\n"
            f"迭代各轮：{json.dumps(runs_brief, ensure_ascii=False)}\n"
            f"迭代状态：{json.dumps(experiment.iteration_state or {}, ensure_ascii=False)}\n"
            f"指标数据：{json.dumps(experiment.metrics or {}, ensure_ascii=False)[:4000]}\n"
            f"{cond_line}"
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

    # voyage 级完成标准（done_criteria）断言该标记：防"过早宣告完成"
    ctx.checkpoint["report_done"] = True
    return {
        "report_chars": len(experiment.report or ""),
        "final_status": final_status,
        "usage": result.usage,
    }
