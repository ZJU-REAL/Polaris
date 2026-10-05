"""experiment voyage 动作（kind ``experiment``，docs/task-system.md §7）。

启动计划：experiment.plan →（compute_budget 闸门）experiment.setup →
         experiment.smoke → experiment.run（第 1 轮）→ experiment.analyze（第 1 轮）
后续轮次由 analyze 的 plan_signal 走引擎确定性分支表动态追加：
         improve/debug → 下一轮 run + analyze；终止 → experiment.figures → experiment.report

约定：
- LLM 只产出 plan JSON / 代码文件内容 / reflection JSON / 绘图脚本 / 报告 markdown，
  远程命令一律走 services/ssh_exec 的白名单模板（LLM 永远不拼 shell）；
- Experiment.status 与步骤联动（awaiting_gate/setup/running/waiting_user/
  reporting/done），每次流转发 WS ``experiment.status``；
- smoke/run 声明 ``on_failure="fail"``：不自动重规划，失败转向用户提问
  （paused_ask，见 docs/task-system.md）；动作异常只留 Activity 痕迹再抛错
  （_guarded），不再抢先把 Experiment 打成 failed——failed 只由人拍板（闸门
  驳回 / 回答「放弃」）。轮次的非零退出码**不是**步骤失败——observation 携带
  exit_code，由 analyze 诊断走 debug 分支；analyze 拿不准时可 decision=ask
  向用户提问，smoke 修复额度用尽同样转提问；
- experiment.run：单轮 launch → 轮询（30s，协作式 cancel / 日志镜像 /
  POLARIS_METRIC + 可选 metrics.json 解析 / 预算超时）→ 主指标 direction 感知比较；
- experiment.analyze：LLM structured reflection → 假设回写 → 终止判定
  （stop/假设定论/无提升/max_runs/max_hours/debug 限额）→ improve/debug 改代码
  → plan_signal（continue/finish）；iteration_state 持续落库；
- experiment.figures：平台写 metrics_all.json → LLM 绘图脚本（只准读该文件）→
  白名单 run_plot → 拉回 figures/*.png(+.pdf) → VLM 质检（失败修脚本 ≤2 次）。
"""

import ast
import asyncio
import contextlib
import copy
import functools
import json
import math
import re
import shlex
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.voyage.actions import ActionContext, register
from app.agents.voyage.errorsig import error_signature, error_text
from app.agents.voyage.runner import Runner, open_runner, parse_container_spec
from app.core.db import get_sessionmaker
from app.core.llm.base import Message
from app.core.voyage_lease import fence_execution
from app.models.activity import Activity
from app.models.base import utcnow
from app.models.experiment import EXPERIMENT_TERMINAL_STATUSES, Experiment, ExperimentRun
from app.models.idea import Idea
from app.models.library_direction import LibraryPaper
from app.models.paper import Paper, PaperWiki
from app.models.resource import Resource, ResourceLease
from app.models.ssh_credential import SSHCredential
from app.models.voyage import VoyageRun, VoyageStep
from app.services import experiment_evidence as evidence_service
from app.services import experiment_settings as experiment_settings_service
from app.services import experiments as experiments_service
from app.services import resource_leases as resource_leases_service
from app.services import ssh_exec
from app.services import voyage_messages as messages_service
from app.services.experiment_metric_stream import MetricLineFramer
from app.services.figure_annotate import prepare_image_for_llm
from app.services.libraries import (
    dedupe_member_rows,
    get_source_library_ids,
    member_papers_stmt,
)
from app.services.managed_commands import (
    CommandAction,
    CommandSnapshot,
    CommandState,
    CommandVerdict,
    FailureReport,
    ModelAssessment,
    OperationContext,
    RecoveryPlan,
    RepairScope,
    adjudicate_command,
    failure_from_snapshot,
    may_apply_recovery_automatically,
)
from app.services.managed_ssh import ManagedCommandHandle
from app.services.runners import registry as runner_registry
from app.services.runners.contract import RunnerPlugin

RUN_POLL_SECONDS = 30.0  # 正式运行轮询间隔（测试 monkeypatch 为 0）
MANAGED_COMMAND_POLL_SECONDS = 2.0
MAX_FIGURE_FIXES = 2  # 绘图脚本执行失败 / VLM 质检不合格的修复次数上限
DEFAULT_NO_IMPROVE_STOP = 2  # 连续 N 轮主指标无提升即停（budget.no_improve_stop 可覆盖）
MAX_QC_IMAGES = 8  # 单次质检最多送 LLM 的图数
_MAX_JSON_ATTEMPTS = 5  # 首次 + 重试 4 次——重试都带错误回喂，只有失败才多花调用
_WIKI_CONTEXT_PAPERS = 6
_WIKI_EXCERPT_CHARS = 600
_LOG_TAIL_FOR_REPORT = 60
_LOG_TAIL_FOR_REFLECTION = 40
_STDERR_CHARS = 2000

COMMAND_ADVISOR_SYSTEM_PROMPT = """\
POLARIS_COMMAND_ADVISOR
You assess one evidence snapshot from a detached remote command. Return JSON only:
{"state":"progressing|slow|stalled|failed|succeeded|unknown","confidence":0.0,
 "reason":"evidence-based explanation","evidence":["observed facts"],
 "proposed_action":"continue_monitoring|extend_observation|run_diagnostic|ask_user_while_running|stop_and_repair",
 "next_check_seconds":30,"safe_to_interrupt":false,"user_message":null}
Use only the supplied snapshot. A timeout checkpoint is not a failure. Changed output or
resource activity is progress. If evidence is insufficient use unknown. Never emit shell
commands. Interruption requires explicit failure or sustained high-confidence zero progress.
"""

RECOVERY_ADVISOR_SYSTEM_PROMPT = """\
POLARIS_RECOVERY_ADVISOR
You diagnose a completed command failure from one structured report. Return JSON only:
{"diagnosis":"root cause","confidence":0.0,
 "repair_scope":"none|connection|infrastructure|dependency_files|application_files|experiment_plan|user_action",
 "proposed_changes":["specific generated files or actions"],
 "expected_evidence":"what proves the fix worked","minimal_retry":"smallest operation to retry",
 "next_step":"concise recommendation for the user"}
Do not invent missing evidence. Infrastructure, connection, authentication and external-service
failures must not propose changes to generated dependency or application files.
"""


class ManagedLaunchCancelled(RuntimeError):
    """Cancellation was observed before a reserved remote launch."""


class ManagedCommandNeedsUser(RuntimeError):
    def __init__(
        self,
        handle: ManagedCommandHandle,
        snapshot: CommandSnapshot,
        assessment: ModelAssessment | None,
        verdict: CommandVerdict,
    ) -> None:
        self.handle = handle
        self.snapshot = snapshot
        self.assessment = assessment
        self.verdict = verdict
        super().__init__(verdict.reason)


class ManagedCommandCancelled(RuntimeError):
    def __init__(
        self,
        snapshot: CommandSnapshot,
        executor: Runner,
    ) -> None:
        self.snapshot = snapshot
        self.executor = executor
        super().__init__("voyage cancelled while remote command was running")


def _serialize_managed_handle(handle: ManagedCommandHandle) -> dict[str, Any]:
    context = handle.context
    return {
        "operation_id": handle.operation_id,
        "attempt_id": handle.attempt_id,
        "process_id": handle.process_id,
        "process_group_id": handle.process_group_id,
        "context": {
            "phase": context.phase,
            "operation": context.operation,
            "display_command": context.display_command,
            "target": context.target,
            "soft_timeout_seconds": context.soft_timeout_seconds,
            "stall_timeout_seconds": context.stall_timeout_seconds,
            "hard_timeout_seconds": context.hard_timeout_seconds,
            "repair_scope": context.repair_scope,
        },
    }


def _restore_managed_handle(data: Any) -> ManagedCommandHandle | None:
    if not isinstance(data, dict) or not isinstance(data.get("context"), dict):
        return None
    try:
        context_data = data["context"]
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
        return ManagedCommandHandle(
            operation_id=str(data["operation_id"]),
            attempt_id=str(data["attempt_id"]),
            context=context,
            process_id=int(data["process_id"]),
            process_group_id=int(data["process_group_id"]),
        )
    except (KeyError, TypeError, ValueError):
        return None


METRIC_LINE_RE = re.compile(r"POLARIS_METRIC\s+(\{.*\})")
_FIGURE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")  # 远端文件名白名单（防目录穿越）

PLAN_SYSTEM_PROMPT = """\
主指标使用完整精确名称（或 primary_metric.selector），禁止模糊名称。
数值可检验假设可增加 criterion={metric,operator,threshold}；
对照判据增加 comparison=baseline_delta,treatment。
不为因果或泛化主张编造数值判据。
若声明 eval_protocol.n_examples，必须同时声明 sample_count_metric，并要求代码输出完成样本计数。
评测源可声明 protected_files，方法修改范围可声明 allowed_files。
你是 Experiment Lab 的实验规划师，基于晋级 idea 与相关 wiki 摘要产出实验计划。
只输出一个 JSON 对象，不要输出任何其他文字或 Markdown 代码块，格式：
{"kind": "eval|training|agent|analysis|other",
 "hypotheses": [{"text": "可检验的假设", "status": "testing"}],
 "repro_strategy": "基线复现策略（官方代码 > 可信第三方 > 自重写 > 仅引用数字）",
 "steps": ["实验步骤 1", "实验步骤 2"],
 "primary_metric": {"name": "主指标名", "direction": "maximize"},
 "conditions": [{"name": "baseline", "role": "baseline", "description": "对照组"},
                {"name": "treatment_a", "role": "treatment", "description": "处理组"}],
 "eval_protocol": {"dataset": "数据集/来源", "split": "评测划分", "metric": "评测指标",
                   "n_examples": 100, "sample_count_metric": "evaluated_examples", "n_samples": 1},
 "datasets": [{"name": "HF数据集名或来源", "purpose": "test|corpus|train", "size_hint": "规模"}],
 "models": [{"ref": "HF模型名或本机绝对路径", "role": "eval|student|teacher|base"}],
 "container": {"image": "预置框架镜像", "gpus": "device=0,1", "shm_size": "16g"},
 "budget_estimate": {"gpu_hours": 2, "runs": 3}}
约束：
- **kind 先给实验归类**（决定平台怎么备环境/怎么跑）：
  eval=评测/基准（跑固定模型或已有产物，产评测指标，通常无需训练）；
  training=需要训练的方法（微调/RL/蒸馏等，需 GPU）；
  agent=智能体任务（跑 agent 策略/工具，看任务成功率/轨迹）；
  analysis=数据分析/消融（处理数据、统计，不训练不评测大模型）；other=以上都不像。
  按研究方案实事求是地分——别默认 training；很多复现是 eval。
- hypotheses 1-5 条且必须可被实验证实/证伪；steps 3-8 条；
- **执行环境 container（可选，训练类/需重型框架时用）**：不要重复造轮子——需要训练框架、
  分布式、vLLM、CUDA 依赖重的实验，优先声明一个**预置 docker 镜像**在容器里跑；镜像已含框架，
  代码只写「框架配置/入口脚本」而非从零训练循环。常见选型（按需求挑，镜像名要真实存在于目标机）：
  · 强化学习/on-policy/GRPO/PPO/蒸馏 → trl 或 verl 系镜像
    （如 `verlai/verl:vllm017.latest`，含 trl/vllm/peft）；
  · 监督微调/LoRA/SFT → LLaMA-Factory 镜像；· 纯评测/benchmark → 轻量镜像 + lm-eval-harness；
  gpus 用 "device=0,1"（选卡）/"all"/"2"（计数）；不需要 GPU 或不需要重型框架时
  **省略 container**（走裸机 venv）。
- primary_metric 必填：name 是评测代码 POLARIS_METRIC 输出的指标名（对照实验里应是主处理组或均值），
  direction 只能取 maximize / minimize；budget_estimate 是对象（至少含 gpu_hours）；
- **对照实验（复现论文常见）**：若研究方案对比多个方法/配置（如 baseline vs 改进），
  必须在 conditions 里列出（恰一个 role=baseline，其余 role=treatment），并把评测协议写进
  eval_protocol（数据集/划分/指标/样本数）、把要用的真实数据集写进 datasets；
  代码将对每个 condition 用同一评测集跑并逐条 POLARIS_METRIC 输出，供平台做对照分析。
- 若是单一配置的调参类实验，conditions/eval_protocol/datasets 可省略。
- **models（可选但强烈建议）**：把实验要用的模型如实列进 models（ref 用 HF 名如
  `Qwen/Qwen3-1.7B`，或本机绝对/家目录路径如 `~/hf/model/...`；role 标 eval/student/teacher/base）。
  平台会**预检**这些模型并把事实记进面板：本机路径不存在会告警；本机模型的架构信息
  （model_type/architectures/配置分节）会被读出来供你和诊断参考——据此确认模型与你的框架/任务相容。
"""

CODE_SYSTEM_PROMPT = """\
你是 Experiment Lab 的实验工程师，为给定实验计划编写可直接运行的代码文件。
只输出一个 JSON 对象，不要输出任何其他文字或 Markdown 代码块，格式：
{"files": {"requirements.txt": "内容", "run.sh": "内容", "train.py": "内容"}}
硬约束：
- 必须包含 requirements.txt 与 run.sh；文件路径必须是相对路径（禁止 .. / 绝对路径 / ~）
- run.sh 必须支持 --smoke 参数：只跑极小样本（如几条数据、1 个模型、去掉耗时条件）快速验证
  代码可跑通；非 smoke 时跑计划里的真实规模。默认用 .venv/bin/python 运行
- **若 plan 声明了 container（在预置镜像里跑）**：镜像已自带框架，run.sh / plot_figures.py
  直接用镜像的 `python`（不是 .venv/bin/python，也别建 venv）；requirements.txt 只列镜像**缺**的
  增量小包（能不加就不加）；模型/数据走已挂载的只读卷（如 /hf），别重复下载大模型
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

现在冒烟测试失败了。先**诊断失败类别与根因**，再决定怎么修——不局限于「改几行代码」，可做**方案级调整**：
- 超时/太慢（冒烟就该极快）→ 把冒烟规模改到极小：更小样本、更少步数、更小或更省显存的配置、
  缩短生成长度、精简耗时依赖；确保 --smoke 很快跑完。
- 依赖/环境（缺包、版本冲突、CUDA/显存、装不上）→ 改 requirements.txt / run.sh：换/装依赖、
  选设备、降 batch、必要时换实现方式绕开装不上的包。
- 模型/框架不兼容（架构不被支持、多模态用于纯文本、加载报错）→ 换兼容的加载方式/框架/模型规格。
- 代码 bug → 修对应逻辑。
先一句话点明诊断，再输出修复后的**完整文件集合**（同上 JSON 格式）。别动评测协议/数据集/主指标口径。
"""
)

# 依赖安装（setup）失败时的方案级修复：和 smoke 的自愈对称——装不上/太慢不该硬崩，而是回 LLM
# 修 requirements.txt / run.sh。聚焦「环境/依赖」这一类，别去改评测规模（那是 smoke 的事）。
SETUP_FIX_SYSTEM_PROMPT = (
    CODE_SYSTEM_PROMPT
    + """\

现在**依赖安装失败**了（venv/pip 或容器内装包）。先**诊断根因**，再修 requirements.txt / run.sh：
- 缺包/装不上 → 换包名或来源、加缺的系统/编译依赖、必要时换实现方式绕开装不上的包。
- 版本冲突 → 放宽/钉住到相容版本，去掉不必要的强约束。
- 太重/太慢/编译超时 → 精简依赖、用更轻的包或预编译 wheel、去掉可选依赖。
- 若用容器（预置镜像已含框架）→ requirements.txt 只留镜像**缺**的增量小包，能不加就不加。
先一句话点明诊断，再输出修复后的**完整文件集合**（同上 JSON 格式）。别动评测协议/数据集/主指标口径。
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


#: 全文渲染的近期尝试数（其余压成一行摘要——上下文预算给记忆与最新代码，不给旧全文）
_ARCHIVE_RECENT_FULL = 2


def _render_attempt_archive(
    archive: list[dict[str, Any]],
    per_file_cap: int = 2000,
    best_file_cap: int = 4000,
    recent_full: int = _ARCHIVE_RECENT_FULL,
    direction: str = "maximize",
) -> str:
    """把历史尝试（源码+得分+轨迹）渲染进迭代 proposer 提示——通用的「先验经验档案」。

    体量有界（docs/task-system.md，对照 Anthropic long-running agent 结论）：
    只有**最优尝试**与**最近 recent_full 次**渲染源码全文（分别截断
    best_file_cap / per_file_cap），更早的尝试压成一行摘要——它们的结论已经
    蒸馏进实验记忆（MEMORY.md），全文重放只会稀释信号。旧行为（全量渲染）
    在 10 轮实验上能吃掉 ~70K 字符且无上限。"""
    if not archive:
        return ""

    def _score(c: dict[str, Any]) -> tuple[int, float]:
        v = c.get("primary_value")
        if c.get("valid") is False or not _is_storable_number(v):
            return (0, float("-inf"))
        return (1, float(v) if direction == "maximize" else -float(v))

    best = max(archive, key=_score)
    if _score(best)[0] == 0:
        best = None
    recent = set(map(id, archive[-max(recent_full, 0) :]))
    parts = [f"历史尝试档案（共 {len(archive)} 次；最优与最近 {recent_full} 次含源码全文）："]
    for c in archive:
        star = " ★迄今最好" if c is best else ""
        delta = c.get("conditions_delta")
        delta_s = json.dumps(delta, ensure_ascii=False) if delta else "—"
        header = (
            f"\n[尝试 seq={c.get('seq')} | 主指标={c.get('primary_value')}{star} | 对照={delta_s}]"
        )
        if c is not best and id(c) not in recent:
            observation = str(c.get("observation") or "")[:200]
            parts.append(header + (f" 观察：{observation}" if observation else ""))
            continue
        parts.append(header)
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


# ---- 实验记忆（docs/task-system.md）：以文件为载体的跨轮持续记忆 ----
#
# 载体是 workdir 根下的 MEMORY.md（人可读、实验脚本可读、前端 code 端点可实时看），
# checkpoint["memory_md"] 是真源镜像（engine 每步持久化；服务器断连时前端仍可读）。
# 平台在关键事件处确定性写入（计划定稿/环境事实/每轮结论/修复额度用尽/用户决策/
# 终止判定），AI 经 reflection.memory_note 自主记笔记；所有实验 LLM 决策点注入
# 记忆尾部——context 再长，跨轮的关键结论也不丢。

MEMORY_REL = "MEMORY.md"
_MEMORY_MAX_CHARS = 40_000  # 镜像总量上限：超出滚动丢弃最旧条目（标题行保留）
_MEMORY_PROMPT_CHARS = 6_000  # 注入 prompt 的尾部预算（新条目优先）
_MEMORY_HEADER = (
    "# 实验记忆\n\n"
    "平台与 AI 共同维护的跨轮记忆：关键决策、环境事实、每轮结论、已证伪路径。\n"
    "实验脚本可直接读取本文件；新条目在文件末尾。\n"
)


def _memory_text(ctx: ActionContext) -> str:
    return str(ctx.checkpoint.get("memory_md") or "")


def _remember(ctx: ActionContext, section: str, text: str) -> None:
    """向实验记忆追加一条（只写 checkpoint 镜像；workdir 副本由 _sync_memory_file 推）。"""
    text = (text or "").strip()
    if not text:
        return
    memory = _memory_text(ctx) or _MEMORY_HEADER
    stamp = utcnow().strftime("%m-%d %H:%M")
    memory += f"\n### [{stamp}] {section}\n{text}\n"
    if len(memory) > _MEMORY_MAX_CHARS:
        tail = memory[-_MEMORY_MAX_CHARS:]
        cut = tail.find("\n### ")
        if cut >= 0:
            tail = tail[cut:]
        memory = _MEMORY_HEADER + "\n（更早的记忆条目已滚动丢弃）\n" + tail
    ctx.checkpoint["memory_md"] = memory


async def _sync_memory_file(ctx: ActionContext, executor: Runner) -> None:
    """把记忆镜像推到 workdir/MEMORY.md（尽力而为：推不动不影响主流程）。"""
    memory = _memory_text(ctx)
    if not memory:
        return
    with contextlib.suppress(Exception):
        await executor.write_files({MEMORY_REL: memory})


def _memory_prompt(ctx: ActionContext) -> str:
    """记忆尾部 → prompt 注入段（有界；无记忆时空串）。"""
    memory = _memory_text(ctx)
    if not memory:
        return ""
    tail = memory[-_MEMORY_PROMPT_CHARS:]
    if len(memory) > _MEMORY_PROMPT_CHARS:
        cut = tail.find("\n### ")
        if cut >= 0:
            tail = "（更早条目省略，见 MEMORY.md）" + tail[cut:]
    return f"实验记忆（MEMORY.md，跨轮持续维护——先读它再决策）：\n{tail}\n"


def _remember_guidance(ctx: ActionContext, params: dict[str, Any]) -> None:
    """把注入本步骤的用户指示记进记忆（按步骤去重：resume 重放/修复循环不重复记）。"""
    guidance = list(params.get("user_guidance") or [])
    if not guidance or ctx.step_id is None:
        return
    key = f"memory_guidance_seen_{ctx.step_id}"
    seen = int(ctx.checkpoint.get(key) or 0)
    if len(guidance) <= seen:
        return
    for text in guidance[seen:]:
        _remember(ctx, "用户指示", str(text)[:300])
    ctx.checkpoint[key] = len(guidance)


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

STACK_GUARD_PROMPT_SECTION = """\

预装环境保护（硬约束）：若依赖装在系统/镜像 Python（dist-packages）而非全新 venv，
镜像自带的核心框架（torch / transformer_engine / triton / flash-attn / CUDA 相关库）
**严禁重装、升级或改版本**——它们按二进制 ABI 配套编译，动其一就会出现
undefined symbol / ImportError。requirements 只补装缺失的轻量依赖；遇到 ABI/导入类
报错时，优先卸载冲突的可选扩展（如 transformer_engine）或回退到镜像既有版本组合，
而不是重装框架。
"""

INTAKE_PROMPT_SECTION = """\

开题问答（创建实验时按想法向用户确认的关键信息，务必遵循）：
{qa}
"""

HF_MIRROR_ENDPOINT = "https://hf-mirror.com"


def _prompt_with_context(base: str, ctx: ActionContext) -> str:
    """按 params.eval_model / hf_mirror / extra_notes 给 system prompt 条件追加段落。"""
    params = _params(ctx)
    parts = [base, STACK_GUARD_PROMPT_SECTION]
    contract = ctx.checkpoint.get("research_contract")
    if contract:
        parts.append("冻结研究契约（摘要不能替代或修改）：\n"
                     + json.dumps(contract, ensure_ascii=False))
    parts.append(ctx.workflow_guidance("experiment.plan"))
    if str(params.get("eval_model") or "").strip():
        parts.append(EVAL_MODEL_PROMPT_SECTION)
    if params.get("hf_mirror"):
        parts.append(HF_MIRROR_PROMPT_SECTION)
    notes = str(params.get("extra_notes") or "").strip()
    if notes:
        parts.append(EXTRA_NOTES_PROMPT_SECTION.format(notes=notes))
    intake = params.get("intake")
    if isinstance(intake, list):
        qa_lines = [
            f"- 问：{str(qa.get('question') or '').strip()}\n"
            f"  答：{str(qa.get('answer') or '').strip()}"
            for qa in intake
            if isinstance(qa, dict) and str(qa.get("answer") or "").strip()
        ]
        if qa_lines:
            parts.append(INTAKE_PROMPT_SECTION.format(qa="\n".join(qa_lines)))
    return "".join(parts) + ctx.evidence_guidance()


def _env_facts_prompt(env_settings: dict[str, Any]) -> str:
    """把「实验设置」里的环境事实拼成一段提示词，附到 codegen 的 user prompt 后面。

    没配任何一项就返回空串（不往提示词里塞噪声）。这段是**事实陈述**而非建议：模型
    对目标机器一无所知，路径全靠猜，猜错的代价是整个 voyage 跑到冒烟才失败。
    """
    lines: list[str] = []
    if env_settings.get("model_root"):
        root = env_settings["model_root"]
        lines.append(
            f"- 本机模型都放在 {root} 下（也可用环境变量 $POLARIS_MODEL_ROOT）。"
            f"引用本机模型必须用这个前缀的完整路径，如 {root.rstrip('/')}/Qwen/Qwen3-1.7B；"
            "不要自己编造别的目录层级。"
        )
    if env_settings.get("dataset_root"):
        root = env_settings["dataset_root"]
        lines.append(f"- 本机数据都放在 {root} 下（环境变量 $POLARIS_DATASET_ROOT）。")
    if env_settings.get("pip_index_url"):
        lines.append(
            f"- pip 镜像源已由平台配好（PIP_INDEX_URL={env_settings['pip_index_url']}），"
            "requirements.txt 里不要再写 -i/--index-url。"
        )
    if env_settings.get("hf_endpoint"):
        lines.append(
            f"- HF 端点已由平台配好（HF_ENDPOINT={env_settings['hf_endpoint']}），"
            "代码里不要再改它。"
        )
    # 自定义变量只报名字：值可能是密钥，不该进提示词；代码按名字从环境里读即可
    names = [v["name"] for v in env_settings.get("env_vars") or [] if v.get("name")]
    if names:
        lines.append(
            "- 平台已导出这些环境变量，需要时从环境里读（如 os.environ[...]），"
            "不要把它们的值写死在代码里：" + "、".join(names) + "。"
        )
    if not lines:
        return ""
    return "\n\n本机环境（平台实配，按此写代码，不要臆测）：\n" + "\n".join(lines)


def diagnose_failure(err_text: str, env_settings: dict[str, Any] | None = None) -> str:
    """把 stderr 里**确定性可辨认**的失败归类，回一句定向提示给修复循环。

    修复循环原本只把 stderr 原样丢回给模型，指望它自己看出问题。对「路径写错」这种
    错，模型看到的是 transformers 抛的 HFValidationError 或 OSError——它不知道这台
    机器上模型到底在哪，于是改来改去还是错。实测（voyage 6c5df454）三次尝试全废在
    同一个不存在的路径 /hf/Qwen/Qwen3-1.7B 上。

    这里只认**签名明确**的几类：错认了就是给条误导的提示，所以宁可少认不可乱认，
    认不出就返回空串（退回原来的行为，让模型自己看 stderr）。
    """
    settings = env_settings or {}
    text = err_text or ""
    lowered = text.lower()

    # ---- 本机路径/模型引用错 ----
    path_signatures = (
        "hfvalidationerror",
        "repo id must be in the form",
        "can't load the configuration of",
        "is not a local folder and is not a valid model identifier",
        "no such file or directory",
        "does not appear to have a file named config.json",
    )
    if any(sig in lowered for sig in path_signatures):
        root = settings.get("model_root")
        if root:
            example = f"{root.rstrip('/')}/Qwen/Qwen3-1.7B"
            return (
                f"引用的模型/文件路径在这台机器上不存在。本机模型的根目录是 {root}"
                f"（环境变量 $POLARIS_MODEL_ROOT），完整路径形如 {example}。"
                "请改成这个前缀下的真实路径，或改用能联网下载的 HF 名（如 Qwen/Qwen3-1.7B，"
                "注意不要给它加本机路径前缀）。不要臆造目录层级。"
            )
        return (
            "引用的模型/文件路径在这台机器上不存在。平台没有配置本机模型根目录，"
            "请改用能联网下载的 HF 名（如 Qwen/Qwen3-1.7B），不要写本机绝对路径。"
        )

    # ---- 缺依赖 ----
    if "modulenotfounderror" in lowered or "no module named" in lowered:
        return (
            "缺 Python 依赖。把缺的包补进 requirements.txt（写明可用版本），"
            "不要在代码里 try/except 掉 import 假装能跑。"
        )

    # ---- 显存不够 ----
    if "out of memory" in lowered or "cuda oom" in lowered:
        return (
            "显存不够。冒烟本来就该极小：换更小的模型/更短的序列/更小的 batch，"
            "或加载时用更省显存的精度。别靠重试碰运气。"
        )

    # ---- 装依赖时网络不通 ----
    network_signatures = (
        "could not find a version",
        "connection to pypi",
        "read timed out",
        "temporary failure in name resolution",
        "network is unreachable",
    )
    if any(sig in lowered for sig in network_signatures):
        index = settings.get("pip_index_url")
        if index:
            return (
                f"装依赖时网络不通。平台已配好镜像源（PIP_INDEX_URL={index}），"
                "requirements.txt 里不要再自己写 -i/--index-url 覆盖它；"
                "也请去掉装不上的可选依赖。"
            )
        return (
            "装依赖时网络不通。请精简依赖、去掉可选包；如果必须联网下载大包，"
            "考虑换成镜像里已自带的实现。"
        )
    return ""


def _platform_env_files(
    ctx: ActionContext,
    *,
    proxy_url: str | None = None,
    no_proxy_extra: str = "",
    env_settings: dict[str, Any] | None = None,
) -> dict[str, str]:
    """平台生成的 env.sh（固定内容，非 LLM 产物）：恒定导出 POLARIS_WORKDIR，
    hf_mirror 时追加 HF_ENDPOINT 镜像；服务器配置了出网代理时导出 http(s)_proxy，
    并把内网 LLM 地址列入 no_proxy（评测 API 不走代理）。模板执行前会 source。

    ``env_settings`` 是「实验设置」里配的全局环境（管理端可改，见
    services/experiment_settings）。这些值已在服务层过白名单校验，此处直接用：
    pip 镜像与 HF 端点导出成环境变量，模型/数据集根目录也导出，方便生成代码引用。
    """
    settings = env_settings or {}
    lines = [
        "export POLARIS_WORKDIR=$(pwd)",
        # 有 venv 就激活：让 LLM 代码里的裸 `python` 落到 venv（很多主机只有 python3，
        # 裸机实验实测 LLM 反复写 `python` 且修复循环绕不开 exit 127；容器模式无 .venv 为 no-op）
        "[ -f .venv/bin/activate ] && . .venv/bin/activate",
    ]
    # 模型/数据集根目录：导出给生成代码用，省得它靠猜路径
    if settings.get("model_root"):
        lines.append(f"export POLARIS_MODEL_ROOT={settings['model_root']}")
    if settings.get("dataset_root"):
        lines.append(f"export POLARIS_DATASET_ROOT={settings['dataset_root']}")
    # pip 镜像：装依赖慢/连不上官方源是实验起不来的常见原因，配了就全局生效
    if settings.get("pip_index_url"):
        lines.append(f"export PIP_INDEX_URL={settings['pip_index_url']}")
    # HF 端点：设置里配的优先于 hf_mirror 参数的内置镜像
    hf_endpoint = settings.get("hf_endpoint") or (
        HF_MIRROR_ENDPOINT if _params(ctx).get("hf_mirror") else ""
    )
    if hf_endpoint:
        lines.append(f"export HF_ENDPOINT={hf_endpoint}")
    # 自定义环境变量：名字已过白名单，值整体加引号（可含空格等，不含换行）
    for var in settings.get("env_vars") or []:
        lines.append(f"export {var['name']}={shlex.quote(var['value'])}")
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
科学判决由平台检查冻结判据。
hypothesis_updates 的 evidence_refs 必须列出本轮/历史有效评估的 evaluation_id。
无有效证据或无判据保持 testing，不能把预算耗尽当 falsified。
你是 Experiment Lab 的实验分析师，基于本轮运行结果做结构化反思并决定下一步。
只输出一个 JSON 对象，不要输出任何其他文字或 Markdown 代码块，格式：
{"observation": "本轮结果观察", "diagnosis": "原因诊断",
 "hypothesis_updates": [{"index": 0, "status": "verified|falsified|testing", "evidence": "证据"}],
 "decision": "improve|debug|stop|ask", "planned_change": "下一轮计划修改", "stop_reason": null,
 "question": null, "memory_note": null}
约束：
- hypothesis_updates 的 index 是假设清单下标（从 0 开始），status 只能取 verified/falsified/testing
- 本轮运行失败（exit_code 非 0）时 decision 用 debug；结果已足以回答全部假设时用 stop
- 本轮失败时，diagnosis 要点明**失败类别**（依赖/环境、模型或框架不兼容、配置/超时/OOM、代码 bug）
  与根因，并在 planned_change 里给出方案级修法（可换依赖/框架/加载方式，不限于改几行代码）
- decision=stop 时 stop_reason 必填一句话；decision=improve 时 planned_change 必填
- **拿不准时用 decision=ask 向用户提问**（question 必填一句具体的问题）：比如结果反常到
  怀疑度量有误、两个改进方向证据相当难以取舍、或继续下去要花大算力而收益存疑。
  能自己判断就别问；问题里给出候选方向让用户好回答
- 若给了「用户指示」，那是用户在对话里的最新意见，**优先遵循**
- memory_note 可选：想跨轮记住的关键结论/教训（一两句，如「X 方向已证伪：原因」），
  会写进实验记忆（MEMORY.md）供后续轮次与收尾报告使用
- 对照实验：若给了「对照汇总」，据 baseline vs treatment 的 delta 判断假设成立与否
  （处理组是否优于 baseline），别只看单个 primary_value；对照结果已清晰时可直接 stop
"""

PLOT_SYSTEM_PROMPT = """\
每条曲线绑定一个 evaluation/run/candidate。不要把不同 run 的同名 step 拼成连续训练轨迹。
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
    if (experiment is None or experiment.project_id != ctx.run.project_id
            or experiment.voyage_id != ctx.run.id):
        raise ValueError("experiment is unavailable in this Voyage")
    return experiment


async def _commit_action(
    session: AsyncSession, ctx: ActionContext, *, publication: bool = False
) -> bool:
    """Fence domain publication in the same transaction as candidate/result writes."""
    with session.no_autoflush:
        await fence_execution(session, ctx.run.id)
        if publication:
            status = await session.scalar(select(VoyageRun.status).where(
                VoyageRun.id == ctx.run.id
            ))
            if status in {"cancelled", "failed"}:
                await session.rollback()
                return False
    await session.commit()
    return True


async def _set_status(
    ctx: ActionContext, session: AsyncSession, experiment: Experiment, status: str
) -> None:
    if experiment.status == status:
        return
    experiment.status = status
    await _commit_action(session, ctx)
    await ctx.notify(
        {"type": "experiment.status", "experiment_id": str(experiment.id), "status": status}
    )


async def _mark_attention(ctx: ActionContext, reason: str) -> None:
    """异常路径：只留 Activity 痕迹，不再抢先把实验打成 failed。

    命运交给引擎的失败分派——原地重试 / 计划调整 / 转向用户提问（paused_ask）
    都可能救回来；提前写死终态后 EXPERIMENT_TERMINAL_STATUSES 检查会挡住一切
    后续状态更新，任务明明救活了实验行却永远躺在 failed。failed 只在两处写：
    闸门驳回联动与用户回答「放弃」（都走 experiments_service.fail_by_voyage）。
    """
    async with get_sessionmaker()() as session:
        experiment = await session.get(Experiment, _experiment_id(ctx))
        if (experiment is None or experiment.status in EXPERIMENT_TERMINAL_STATUSES
                or experiment.project_id != ctx.run.project_id
                or experiment.voyage_id != ctx.run.id):
            return
        session.add(
            Activity(
                project_id=experiment.project_id,
                actor="agent:experiment",
                kind="experiment.attention",
                message=f"实验步骤出错：{reason[:300]}",
                payload={"experiment_id": str(experiment.id), "reason": reason[:1000]},
            )
        )
        await _commit_action(session, ctx)


def _guidance_line(params: dict[str, Any]) -> str:
    """params["user_guidance"] → prompt 注入行（无建议时空串）。

    每个 LLM 决策点都必须带上这一行——线上实测过：建议只接了 smoke/analyze，
    用户对着装依赖循环连发建议全被无视（docs/task-system.md）。
    """
    guidance = params.get("user_guidance")
    if not guidance:
        return ""
    return f"用户指示（务必优先遵循）：{json.dumps(guidance, ensure_ascii=False)}\n"


async def _refresh_user_guidance(ctx: ActionContext, params: dict[str, Any]) -> str:
    """长动作（setup/smoke 修复循环）每一轮把对话流里**新到**的建议并入 guidance。

    引擎只在步骤开始时注入一次；装依赖/修代码动辄几十分钟，期间用户发的建议
    不该等到下一个步骤边界才被看见。消费标记与「并入步骤 params」同一事务提交
    （resume 重放不会重复注入也不会丢建议）；失败静默降级为用已有 guidance。
    返回最新的 prompt 注入行。
    """
    try:
        async with get_sessionmaker()() as session:
            pending = await messages_service.pending_chat_messages(session, ctx.run.id)
            if pending:
                merged = list(params.get("user_guidance") or []) + [m.text for m in pending]
                step = await session.get(VoyageStep, ctx.step_id) if ctx.step_id else None
                if step is not None:
                    step_params = dict(step.params or {})
                    step_params["user_guidance"] = merged
                    step.params = step_params
                messages_service.mark_chat_consumed(pending, step_id=ctx.step_id)
                await _commit_action(session, ctx)
                params["user_guidance"] = merged
                await ctx.log(f"已收到你的 {len(pending)} 条建议，立即用于当前修复")
    except Exception:  # noqa: BLE001 — 建议拉取失败不能影响修复主流程
        pass
    return _guidance_line(params)


def _guarded(func):
    """动作异常时留 Activity 痕迹再抛给 helm（helm 记 observation.error，
    引擎分派决定重试 / 调整计划 / 向用户提问）。"""

    @functools.wraps(func)
    async def wrapper(ctx: ActionContext, params: dict[str, Any]) -> dict[str, Any]:
        try:
            return await func(ctx, params)
        except asyncio.CancelledError:
            raise
        except ManagedLaunchCancelled:
            return {"cancelled": True}
        except Exception as e:
            await _mark_attention(ctx, error_text(e))
            raise

    return wrapper


def _extract_json(content: str) -> Any:
    start = content.find("{")
    end = content.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object found")
    return json.loads(content[start : end + 1])


async def _complete_json(ctx: ActionContext, *, system: str, user: str, validate) -> Any:
    """stage=experiment 的 LLM JSON 请求：解析/校验失败**带着错误**重试，仍失败抛 ValueError。

    重试必须把上一次的错误回喂给模型。原来是原样重发同一个 prompt——对确定性错误
    （少一个必需文件、生成的 .py 有语法错）这等于让模型再猜一遍同样的题，三次尝试
    烧三次 token 换回同一个错。带上错误后它才知道要改哪里。

    截断（finish_reason=max_tokens）单独处理：JSON 烂在尾部不是模型写错格式，
    重发同 prompt 必然在同一处再截一刀。升档输出预算并要求紧凑输出后重试。
    """
    last_error: Exception | None = None
    max_tokens: int | None = None
    for attempt in range(_MAX_JSON_ATTEMPTS):
        prompt = user
        if last_error is not None:
            hint = ""
            if "语法错误" in str(last_error):
                # 线上高频根因：代码经 JSON 字符串转义后被写坏（反斜杠续行/转义错位）。
                # 光回喂错误行模型常在原地打转，点破成因才改得动。
                hint = (
                    "\n常见根因：代码放进 JSON 字符串时转义出错（多余或缺失的反斜杠、"
                    "\\n 与真实换行混用、行尾反斜杠续行）。请重写出错行附近的代码，"
                    "避免行尾反斜杠与复杂转义写法（如把长 f-string 拆成多次拼接）。"
                )
            prompt = (
                f"{user}\n\n---\n"
                f"上一次输出没通过校验（第 {attempt} 次尝试）：{last_error}{hint}\n"
                "请针对这个错误修正后重新输出完整 JSON，不要重复同样的问题。"
            )
        result = await ctx.llm.complete(
            "experiment",
            [Message(role="system", content=system), Message(role="user", content=prompt)],
            user_id=ctx.run.created_by,
            project_id=ctx.run.project_id,
            voyage_id=ctx.run.id,
            max_tokens=max_tokens,
        )
        if getattr(result, "finish_reason", None) == "max_tokens":
            max_tokens = 16384 if max_tokens else 8192
            last_error = ValueError(
                "输出超长被截断（max_tokens）。请压缩输出：省略非必要注释与空行，"
                "避免重复内容，只保留任务要求的字段与文件"
            )
            continue
        try:
            return validate(_extract_json(result.content))
        except (ValueError, TypeError, KeyError, json.JSONDecodeError) as e:
            last_error = e
    raise ValueError(f"LLM 连续输出非法 JSON：{last_error}")


def _validate_command_assessment(data: Any) -> ModelAssessment:
    if not isinstance(data, dict):
        raise ValueError("command assessment must be an object")
    evidence = data.get("evidence")
    return ModelAssessment(
        state=CommandState(str(data.get("state") or "unknown")),
        confidence=max(0.0, min(float(data.get("confidence") or 0), 1.0)),
        reason=str(data.get("reason") or "no reason supplied")[:1000],
        evidence=tuple(str(item)[:500] for item in evidence[:12])
        if isinstance(evidence, list)
        else (),
        proposed_action=CommandAction(str(data.get("proposed_action") or "continue_monitoring")),
        next_check_seconds=max(5.0, min(float(data.get("next_check_seconds") or 30), 600.0)),
        safe_to_interrupt=data.get("safe_to_interrupt") is True,
        user_message=str(data["user_message"])[:1000] if data.get("user_message") else None,
    )


async def _assess_managed_command(
    ctx: ActionContext, snapshot: CommandSnapshot
) -> ModelAssessment | None:
    try:
        result = await ctx.llm.complete(
            "experiment",
            [
                Message(role="system", content=COMMAND_ADVISOR_SYSTEM_PROMPT),
                Message(role="user", content=json.dumps(snapshot.to_dict(), ensure_ascii=False)),
            ],
            user_id=ctx.run.created_by,
            project_id=ctx.run.project_id,
            voyage_id=ctx.run.id,
        )
        assessment = _validate_command_assessment(_extract_json(result.content))
        await ctx.log(
            f"远端命令判断：{snapshot.context.phase} state={assessment.state} "
            f"confidence={assessment.confidence:.2f} action={assessment.proposed_action}"
        )
        return assessment
    except Exception as exc:  # noqa: BLE001 - policy handles missing advice
        await ctx.log(f"远端命令判断不可用，保留远端进程：{type(exc).__name__}")
        return None


def _validate_recovery_plan(data: Any) -> tuple[RecoveryPlan, str]:
    if not isinstance(data, dict):
        raise ValueError("recovery plan must be an object")
    proposed = data.get("proposed_changes")
    plan = RecoveryPlan(
        diagnosis=str(data.get("diagnosis") or "unknown")[:2000],
        confidence=max(0.0, min(float(data.get("confidence") or 0), 1.0)),
        repair_scope=RepairScope(str(data.get("repair_scope") or "none")),
        proposed_changes=tuple(str(item)[:500] for item in proposed[:20])
        if isinstance(proposed, list)
        else (),
        expected_evidence=str(data.get("expected_evidence") or "")[:1000],
        minimal_retry=str(data.get("minimal_retry") or "")[:200],
    )
    return plan, str(data.get("next_step") or plan.diagnosis)[:1500]


async def _plan_failure_recovery(
    ctx: ActionContext, report: FailureReport
) -> tuple[RecoveryPlan | None, str]:
    try:
        result = await ctx.llm.complete(
            "experiment",
            [
                Message(role="system", content=RECOVERY_ADVISOR_SYSTEM_PROMPT),
                Message(
                    role="user",
                    content=json.dumps(report.to_dict(), ensure_ascii=False, default=str),
                ),
            ],
            user_id=ctx.run.created_by,
            project_id=ctx.run.project_id,
            voyage_id=ctx.run.id,
        )
        return _validate_recovery_plan(_extract_json(result.content))
    except Exception as exc:  # noqa: BLE001 - real failure remains visible
        return None, f"自动诊断不可用：{type(exc).__name__}: {exc}"


async def _reserve_managed_launch(
    ctx: ActionContext, session: AsyncSession, executor: Runner,
    experiment: Experiment, *, phase: str, operation: str, command: str,
) -> str | None:
    """Persist launch intent before slow I/O, including setup and smoke phases."""
    state = _iteration_state(experiment)
    attempt_id = str(uuid.uuid4())
    state.setdefault("execution_started_at", utcnow().isoformat())
    state["remote_cleanup"] = {
        "status": "pending", "reason": "launching", "launch_attempt_id": attempt_id,
        "workspace_id": getattr(executor, "run_workspace_id", None),
        "operation_context": {"phase": phase, "operation": operation,
                              "display_command": command},
    }
    experiment.iteration_state = state
    return attempt_id if await _commit_action(session, ctx, publication=True) else None


async def _run_managed_phase(
    ctx: ActionContext, session: AsyncSession, executor: Runner, experiment: Experiment,
    *, phase: str, operation: str, command: str, launch_method: str,
    reconcile_only: bool = False,
) -> tuple[CommandSnapshot | None, Runner, dict[str, Any] | None]:
    """Use durable identity for repair and delivery commands as well as initial setup."""
    desired_workspace = getattr(executor, "run_workspace_id", None)
    cleanup = (_iteration_state(experiment).get("remote_cleanup") or {})
    handle = _restore_managed_handle(cleanup.get("handle"))
    intent = cleanup.get("operation_context")
    if cleanup and handle is None and isinstance(intent, dict):
        executor.bind_run_workspace(cleanup.get("workspace_id"))
        recovered = await executor.recover_managed_command(OperationContext(
            phase=str(intent["phase"]), operation=str(intent["operation"]),
            display_command=str(intent["display_command"]), target=experiment.server_host,
        ))
        if recovered and recovered.attempt_id == cleanup.get("launch_attempt_id"):
            handle = recovered
    if cleanup and handle is None:
        return None, executor, {"remote_operation_continues": True, "ask": {
            "ask_kind": "action_ask", "question":
            "已有远端操作的身份尚不能核对，保留资源并等待对账后再继续。",
        }}
    if handle is not None:
        executor.bind_run_workspace(cleanup.get("workspace_id"))
    else:
        if reconcile_only:
            return None, executor, None
        executor.bind_run_workspace(desired_workspace)
        attempt_id = await _reserve_managed_launch(
            ctx, session, executor, experiment, phase=phase, operation=operation, command=command,
        )
        if attempt_id is None:
            return None, executor, {"cancelled": True}
        handle = await getattr(executor, launch_method)(attempt_id=attempt_id)
    try:
        snapshot, executor = await _monitor_managed_command(
            ctx, session, executor, experiment, handle
        )
    except ManagedCommandNeedsUser as pending:
        return None, executor, _managed_command_waiting_result(ctx, experiment, pending)
    except ManagedCommandCancelled as cancelled:
        return None, cancelled.executor, {"cancelled": True}
    if reconcile_only:
        executor.bind_run_workspace(desired_workspace)
        ctx.checkpoint.pop("managed_command_waiting", None)
        return snapshot, executor, None
    if (handle.operation_id != operation
            or getattr(executor, "run_workspace_id", None) != desired_workspace):
        executor.bind_run_workspace(desired_workspace)
        return await _run_managed_phase(
            ctx, session, executor, experiment, phase=phase, operation=operation,
            command=command, launch_method=launch_method,
        )
    ctx.checkpoint.pop("managed_command_waiting", None)
    return snapshot, executor, None


async def _monitor_managed_command(
    ctx: ActionContext,
    session: AsyncSession,
    executor: Runner,
    experiment: Experiment,
    handle: ManagedCommandHandle,
    run: ExperimentRun | None = None,
) -> tuple[CommandSnapshot, Runner]:
    stdout_offset = 0
    stderr_offset = 0
    previous_token: str | None = None
    silent_extensions = 0
    diagnostics_run = 0
    diagnostic_evidence: dict[str, str] | None = None
    next_assessment_at = 0.0
    reconnect_streak = 0
    framer = MetricLineFramer()
    state = _iteration_state(experiment)
    state.setdefault("execution_started_at", utcnow().isoformat())
    state["remote_cleanup"] = {
        "status": "pending", "reason": "running",
        "workspace_id": getattr(executor, "run_workspace_id", None),
        "handle": _serialize_managed_handle(handle),
    }
    experiment.iteration_state = state
    await _commit_action(session, ctx)

    async def record_cleanup(confirmed: bool, reason: str) -> None:
        state = _iteration_state(experiment)
        if confirmed:
            state.pop("remote_cleanup", None)
            ctx.checkpoint.pop("managed_command_waiting", None)
        else:
            state["remote_cleanup"] = {
                "status": "pending", "reason": reason,
                "workspace_id": getattr(executor, "run_workspace_id", None),
                "handle": _serialize_managed_handle(handle),
            }
        experiment.iteration_state = state
        await _commit_action(session, ctx)

    async def ingest_output(*, terminal: bool = False) -> None:
        nonlocal stdout_offset, stderr_offset
        while True:
            old_offsets = (stdout_offset, stderr_offset)
            chunks, stdout_offset, stderr_offset = await executor.read_managed_output(
                handle, stdout_offset=stdout_offset, stderr_offset=stderr_offset,
            )
            for chunk in chunks:
                experiments_service.append_terminal_output(
                    experiment.id, operation=handle.operation_id,
                    stream=chunk.stream, text=chunk.text,
                )
                if run is not None:
                    experiments_service.append_local_log(experiment.id, run.seq, chunk.text)
                    points = _evaluation_metric_lines(framer.feed(chunk.stream, chunk.text))
                    run.metrics = merge_metrics(run.metrics, points)
            if not terminal or old_offsets == (stdout_offset, stderr_offset):
                break
        if terminal and run is not None:
            run.metrics = merge_metrics(run.metrics, _evaluation_metric_lines(framer.finish()))
        if run is not None:
            await _commit_action(session, ctx)

    def append_lifecycle(message: str) -> None:
        experiments_service.append_terminal_output(
            experiment.id,
            operation=handle.operation_id,
            stream="system",
            text=message,
        )

    append_lifecycle(
        f"monitoring attempt {handle.attempt_id} (pid={handle.process_id}): "
        f"{handle.context.display_command}"
    )

    async def reconnect() -> None:
        nonlocal executor
        with contextlib.suppress(Exception):
            await executor.close()
        workspace_id = getattr(executor, "run_workspace_id", None)
        executor = await _open_executor(session, ctx, experiment)
        if workspace_id:
            executor.bind_run_workspace(workspace_id)

    while True:
        try:
            await ingest_output()
            snapshot = await executor.inspect_managed_command(
                handle,
                previous_token=previous_token,
                diagnostic_evidence=diagnostic_evidence,
            )
        except Exception as exc:  # noqa: BLE001 - reconnect never restarts command
            if not ssh_exec.is_connection_error(exc):
                raise
            reconnect_streak += 1
            session.add(
                Activity(
                    project_id=ctx.run.project_id,
                    actor="system:voyage",
                    kind="experiment.ssh_reconnect",
                    message=(
                        "Managed command polling lost SSH; reconnecting "
                        f"(attempt {reconnect_streak}): {type(exc).__name__}"
                    ),
                    payload={
                        "experiment_id": str(experiment.id),
                        "operation_id": handle.operation_id,
                        "attempt_id": handle.attempt_id,
                        "run_seq": run.seq if run is not None else None,
                        "attempt": reconnect_streak,
                    },
                )
            )
            await _commit_action(session, ctx)
            append_lifecycle(
                f"SSH connection lost; reconnecting to the same managed attempt "
                f"({reconnect_streak}): {type(exc).__name__}"
            )
            await reconnect()
            await asyncio.sleep(_reconnect_backoff(reconnect_streak))
            continue

        reconnect_streak = 0

        if await _voyage_cancelled(session, ctx):
            append_lifecycle("voyage cancelled; stopping the current remote command")
            stopped = await executor.stop_managed_command(handle)
            await record_cleanup(bool(stopped), "cancel_stop_unconfirmed")
            snapshot.process_alive = not stopped
            if stopped and snapshot.exit_status is None:
                snapshot.exit_status = -15
            snapshot.stderr_tail = snapshot.stderr_tail or (
                "remote command stopped after the voyage was cancelled"
                if stopped
                else "voyage was cancelled but the remote command could not be verified stopped"
            )
            if run is not None and stopped:
                await ingest_output(terminal=True)
                run.exit_code = snapshot.exit_status
                run.status = "failed"
                run.finished_at = utcnow()
                await _commit_action(session, ctx)
            await session.refresh(experiment)
            if experiment.status not in EXPERIMENT_TERMINAL_STATUSES:
                await _set_status(ctx, session, experiment, "cancelled")
            raise ManagedCommandCancelled(snapshot, executor)

        if snapshot.output_changed:
            silent_extensions = 0
            diagnostics_run = 0
            diagnostic_evidence = None
        previous_token = snapshot.progress_token
        if snapshot.exit_status is not None:
            if snapshot.process_alive:
                stopped = await executor.stop_managed_command(handle)
                await record_cleanup(bool(stopped), "descendants_stop_unconfirmed")
                if not stopped:
                    raise ManagedCommandNeedsUser(handle, snapshot, None, CommandVerdict(
                        CommandAction.ASK_USER_WHILE_RUNNING,
                        "exit recorded but remote descendants are not confirmed stopped",
                    ))
                snapshot.process_alive = False
            await ingest_output(terminal=True)
            await record_cleanup(True, "completed")
            append_lifecycle(f"command completed with exit status {snapshot.exit_status}")
            return snapshot, executor
        if not snapshot.process_alive:
            final = await executor.inspect_managed_command(handle, previous_token=previous_token)
            if final.exit_status is None:
                final.exit_status = -1
                final.stderr_tail = final.stderr_tail or (
                    "remote process disappeared without recording an exit status"
                )
            append_lifecycle(f"command process disappeared with exit status {final.exit_status}")
            await ingest_output(terminal=True)
            await record_cleanup(True, "process_gone")
            return final, executor

        hard_reached = bool(
            snapshot.context.hard_timeout_seconds
            and snapshot.elapsed_seconds >= snapshot.context.hard_timeout_seconds
        )
        started = (_iteration_state(experiment).get("execution_started_at")
                   or (ctx.checkpoint.get("iterate") or {}).get("started_at"))
        max_hours = float((experiment.budget or {}).get("max_hours") or 0)
        hard_reached = hard_reached or bool(
            started and max_hours
            and _elapsed_hours(datetime.fromisoformat(str(started))) >= max_hours
        )
        if hard_reached:
            stopped = await executor.stop_managed_command(handle)
            await record_cleanup(bool(stopped), "hard_deadline_stop_unconfirmed")
            if not stopped:
                raise ManagedCommandNeedsUser(handle, snapshot, None, CommandVerdict(
                    CommandAction.ASK_USER_WHILE_RUNNING,
                    "hard deadline reached; remote stop remains unconfirmed",
                ))
            await ingest_output(terminal=True)
            snapshot.process_alive = False
            snapshot.exit_status = -15
            snapshot.stderr_tail = "stopped at the absolute experiment hard deadline"
            return snapshot, executor

        soft_reached = bool(
            snapshot.context.soft_timeout_seconds
            and snapshot.elapsed_seconds >= snapshot.context.soft_timeout_seconds
        )
        stalled = bool(
            snapshot.context.stall_timeout_seconds
            and snapshot.seconds_since_output >= snapshot.context.stall_timeout_seconds
        )
        if (soft_reached or stalled) and asyncio.get_running_loop().time() >= next_assessment_at:
            assessment = await _assess_managed_command(ctx, snapshot)
            verdict = adjudicate_command(
                snapshot,
                assessment,
                silent_extensions=silent_extensions,
                diagnostics_run=diagnostics_run,
            )
            next_assessment_at = asyncio.get_running_loop().time() + verdict.next_check_seconds
            if verdict.action == CommandAction.ASK_USER_WHILE_RUNNING:
                append_lifecycle(
                    f"command remains running; waiting for user decision: {verdict.reason}"
                )
                raise ManagedCommandNeedsUser(handle, snapshot, assessment, verdict)
            if verdict.action == CommandAction.RUN_DIAGNOSTIC:
                append_lifecycle("collecting read-only diagnostics before the next decision")
                diagnostic_evidence = await executor.diagnose_managed_command(handle)
                diagnostics_run += 1
            elif verdict.action == CommandAction.EXTEND_OBSERVATION:
                silent_extensions += 1
            elif verdict.action == CommandAction.STOP_AND_REPAIR:
                append_lifecycle("stopping command after a high-confidence stalled assessment")
                stopped = await executor.stop_managed_command(handle)
                await record_cleanup(bool(stopped), "stall_stop_unconfirmed")
                if not stopped:
                    raise ManagedCommandNeedsUser(handle, snapshot, assessment, verdict)
                snapshot.process_alive = False
                snapshot.exit_status = -1
                snapshot.stderr_tail = snapshot.stderr_tail or (
                    "stopped after a confirmed sustained stall"
                )
                await ingest_output(terminal=True)
                return snapshot, executor

        await asyncio.sleep(MANAGED_COMMAND_POLL_SECONDS)


def _managed_command_waiting_result(
    ctx: ActionContext,
    experiment: Experiment,
    pending: ManagedCommandNeedsUser,
) -> dict[str, Any]:
    handle_data = _serialize_managed_handle(pending.handle)
    ctx.checkpoint["managed_command_waiting"] = handle_data
    assessment = pending.assessment
    question = (
        assessment.user_message
        if assessment and assessment.user_message
        else (
            f"远端命令 {pending.handle.context.display_command} 仍在运行，但当前无法可靠判断"
            f"是否继续推进：{pending.verdict.reason}。远端进程未被中断。"
        )
    )
    return {
        "workdir": experiment.workdir,
        "remote_operation_continues": True,
        "managed_command": pending.snapshot.to_dict(),
        "ask": {
            "ask_kind": "managed_command",
            "question": question,
            "context": {
                "remote_operation_continues": True,
                "handle": handle_data,
                "snapshot": pending.snapshot.to_dict(),
                "assessment": {
                    "state": assessment.state,
                    "confidence": assessment.confidence,
                    "reason": assessment.reason,
                    "evidence": assessment.evidence,
                }
                if assessment
                else None,
                "safety_verdict": pending.verdict.reason,
            },
            "options": [
                {
                    "id": "retry",
                    "zh": "保持运行并恢复监控",
                    "en": "Keep running and resume monitoring",
                },
                {
                    "id": "stop_remote",
                    "zh": "停止当前命令并根据错误修复",
                    "en": "Stop this command and repair",
                },
            ],
        },
    }


async def _resolve_runner_plugin(
    session: AsyncSession, ctx: ActionContext, experiment: Experiment
) -> tuple[RunnerPlugin, Runner | None]:
    """Runner v2 分派点（#716）：backend 从 checkpoint.params 读（#676 创建时已存），
    经注册表拿插件。这是动作层「获得执行器/子基座」的唯一入口。

    - python-ml（含缺省）：插件绑定的底座就是原 open_runner 的返回物——v2 适配器的
      生命周期方法委托同一套 v1 原语，动作层继续直连原语（launch_setup/run_smoke/
      probe_gpu 等未进 v2 生命周期的观测与修复面），语义与直接 open_runner 等价，
      python-ml 路径行为逐字节不变；
    - 非 SSH 底座后端（credential_kinds 不含 "ssh"，如 ngspice/openfoam/fmu）：
      返回未绑底座的插件与 None——分派正确性在此保证；动作层按 v2 生命周期
      驱动这些后端是后续阶段（R4 余下部分）的事。
    """
    raw = str(_params(ctx).get("backend") or "").strip()
    backend = raw or runner_registry.DEFAULT_BACKEND
    # 未注册后端在此抛 UnknownBackendError：经 _guarded 留痕后交引擎失败分派（可诊断）
    manifest = runner_registry.manifest_for(backend)
    if "ssh" not in manifest.credential_kinds:
        return runner_registry.get(backend)(), None
    if experiment.credential_id is None:
        raise ValueError("实验缺少 SSH 凭据（credential_id 为空）")
    credential = await session.get(SSHCredential, experiment.credential_id)
    if credential is None:
        raise ValueError("SSH 凭据已删除，无法连接实验服务器")
    plan = experiment.plan if isinstance(experiment.plan, dict) else {}
    runner = await open_runner(
        credential=credential,
        exp_id=str(experiment.id),
        project_id=experiment.project_id,
        kind=plan.get("kind"),
        container=plan.get("container"),
    )
    return runner_registry.get(backend)(runner=runner), runner


async def _open_executor(
    session: AsyncSession, ctx: ActionContext, experiment: Experiment
) -> Runner:
    """为实验打开执行后端（Runner）。分派收窄到 _resolve_runner_plugin 一处（#716）。

    本动作层的既有流程（setup/smoke/run/figures 及其修复循环）建立在 19 原语之上，
    只对 SSH 底座后端成立；非 SSH 后端在这里明确报错（可诊断、进失败分派），
    绝不拿错底座乱跑。
    """
    plugin, runner = await _resolve_runner_plugin(session, ctx, experiment)
    if runner is None:
        raise ValueError(
            f"后端 {plugin.manifest.backend} 不使用 SSH 执行底座，"
            "实验动作流程尚未接入该后端的执行生命周期"
        )
    return runner


# ---- 计划 schema 校验 ----

_HYP_STATUSES = ("testing", "verified", "falsified")
_PM_DIRECTIONS = ("maximize", "minimize")
_DECISIONS = ("improve", "debug", "stop", "ask")
# 实验类型：驱动执行后端(Runner)与策略选择——eval 评测 / training 训练 / agent 智能体任务 /
# analysis 数据分析 / other 其它。plan 归类，向后兼容缺省 other。
_EXPERIMENT_KINDS = ("eval", "training", "agent", "analysis", "other")


def validate_plan(data: Any) -> dict[str, Any]:
    """严格校验 plan JSON：hypotheses / repro_strategy / steps / primary_metric /
    budget_estimate 缺一不可（primary_metric 为 docs/task-system.md §7 新增必填）。"""
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
        item = {"text": text.strip(), "status": "testing"}
        if isinstance(hyp, dict) and isinstance(hyp.get("criterion"), dict):
            item["criterion"] = hyp["criterion"]
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
    # kind 归类：驱动执行后端(Runner)与策略选择；缺失/非法回退 other（不阻断，向后兼容）。
    kind = data.get("kind")
    kind = kind if kind in _EXPERIMENT_KINDS else "other"
    out: dict[str, Any] = {
        "kind": kind,
        "hypotheses": hypotheses,
        "repro_strategy": repro.strip(),
        "steps": steps,
        "primary_metric": {"name": pm_name.strip(), "direction": pm_direction},
        "budget_estimate": budget,
    }
    if "min_delta" in pm:
        if not _is_storable_number(pm["min_delta"]) or pm["min_delta"] < 0:
            raise ValueError("primary_metric min_delta must be finite and non-negative")
        out["primary_metric"]["min_delta"] = float(pm["min_delta"])
    if "selector" in pm:
        if not isinstance(pm["selector"], str) or not pm["selector"].strip():
            raise ValueError("primary_metric selector must be an exact metric name")
        out["primary_metric"]["selector"] = pm["selector"].strip()
    if isinstance(data.get("evaluator_digest"), str):
        out["evaluator_digest"] = data["evaluator_digest"]
    for key in ("allowed_files", "protected_files"):
        if key in data:
            if not isinstance(data[key], list):
                raise ValueError(f"{key} must be a list")
            out[key] = [ssh_exec._validate_relpath(str(name)) for name in data[key]]
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
    # 模型清单（资源预检消费）：规范成 [{ref, role}]，ref 非空才留；ref 过主机路径白名单
    # （本机路径要能安全拼进 cat/test；HF id 也走同一白名单，均无 shell 元字符）。
    raw_models = data.get("models")
    if isinstance(raw_models, list):
        models = []
        for m in raw_models:
            ref = str(m.get("ref") or "").strip() if isinstance(m, dict) else str(m or "").strip()
            if not ref or not ssh_exec._HOST_PATH_RE.match(ref) or ".." in ref:
                continue
            role = m.get("role") if isinstance(m, dict) else None
            models.append({"ref": ref, "role": str(role).strip() if role else ""})
        if models:
            out["models"] = models
    # 容器执行规格（训练类/需框架的实验声明预置镜像）：严格校验后存回 plan，
    # 决定运行时用 ContainerRunner 还是裸机；非法/缺 image → 不存（退回裸机）。
    spec = parse_container_spec(data.get("container"))
    if spec is not None:
        out["container"] = {
            "image": spec.image,
            "gpus": spec.gpus,
            "shm_size": spec.shm_size,
            "mounts": spec.mounts,
        }
    return out


def _check_python_syntax(files: dict[str, str]) -> None:
    """把生成的 .py 编译一遍；语法错在这里就打回，绝不让它上机器。

    实测一次失败（voyage ae147dec）：生成的 train.py 在 f-string 里写了 ``\\"``，
    ``SyntaxError: unexpected character after line continuation character``。这个错
    以前一路穿过校验、rsync 到远端、直到冒烟测试才炸——代价是一次 SSH 往返、三次冒烟
    尝试、两次 LLM 修复调用，最后整个 voyage 判死。而 ``ast.parse`` 在本地零成本就能
    发现它，错误还能顺着 _complete_json 的重试回喂给模型自己改。
    """
    for name, content in sorted(files.items()):
        if not name.endswith(".py"):
            continue
        try:
            ast.parse(content, filename=name)
        except SyntaxError as e:
            # 带上行号与出错行，模型才改得准
            line = (e.text or "").strip()
            where = f"{name}:{e.lineno}" if e.lineno else name
            detail = f"{where}: {e.msg}"
            raise ValueError(
                f"生成的 Python 有语法错误 —— {detail}" + (f"\n出错行：{line}" if line else "")
            ) from e


def validate_files(data: Any) -> dict[str, str]:
    """代码文件 dict 校验：必需文件齐全、路径过白名单、生成的 Python 语法可编译。"""
    files = data.get("files") if isinstance(data, dict) else None
    if not isinstance(files, dict) or not files:
        raise ValueError('expected {"files": {...}}')
    normalized: dict[str, str] = {}
    reserved = {"metrics.json", "run.log", "run.exit", "env.sh", "llm_config.json",
                "metrics_all.json", "result_bundle.json", "MEMORY.md"}
    for name, content in files.items():
        rel = ssh_exec._validate_relpath(str(name))
        if rel in reserved or rel.split("/")[0] in {".polaris", ".venv", "data_cache"}:
            raise ValueError(f"reserved experiment path: {rel}")
        normalized[rel] = str(content)
    for required in ("requirements.txt", "run.sh"):
        if required not in normalized:
            raise ValueError(f"missing required file: {required}")
    if "--smoke" not in normalized["run.sh"]:
        raise ValueError("run.sh must support --smoke argument")
    _check_python_syntax(normalized)
    return normalized


def validate_reflection(data: Any) -> dict[str, Any]:
    """structured reflection 严格校验（docs/task-system.md §7（原 api-m5-a.md §1））。"""
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
        refs = upd.get("evidence_refs")
        if refs is not None:
            if not isinstance(refs, list) or not all(isinstance(ref, str) for ref in refs):
                raise ValueError("hypothesis_update evidence_refs must be strings")
            updates[-1]["evidence_refs"] = refs
    decision = data.get("decision")
    if decision not in _DECISIONS:
        raise ValueError("decision must be improve|debug|stop|ask")
    planned_change = data.get("planned_change")
    stop_reason = data.get("stop_reason")
    question = data.get("question")
    if decision == "ask" and not (isinstance(question, str) and question.strip()):
        raise ValueError('decision "ask" requires a non-empty "question"')
    memory_note = data.get("memory_note")
    return {
        "observation": observation.strip(),
        "diagnosis": diagnosis.strip(),
        "hypothesis_updates": updates,
        "decision": decision,
        "planned_change": str(planned_change).strip() if planned_change else None,
        "stop_reason": str(stop_reason).strip() if stop_reason else None,
        "question": str(question).strip() if question else None,
        "memory_note": str(memory_note).strip() if memory_note else None,
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


def _is_storable_number(value: Any) -> bool:
    """能不能落库：必须是有限的数（NaN / ±Infinity 一律不收）。

    实验算出 NaN 是家常便饭（某个条件下指标无定义、除零、空集求均值）。但 Python 的
    ``json.loads`` **默认接受裸 NaN/Infinity 词元**，``json.dumps`` 也照原样吐出来——
    而它们都不是合法 JSON，写进 JSONB 时 Postgres 直接拒收：

        asyncpg.exceptions.InvalidTextRepresentationError:
        invalid input syntax for type json  DETAIL: Token "NaN" is invalid.

    实测（voyage 6c5df454 第 1 轮运行）：实验跑完了、指标也产出了，就因为其中一项是
    NaN，整条 UPDATE 失败，这一轮判死。一个无定义的指标点本来就不承载信息，丢掉它
    远比让整轮成果陪葬合理。bool 是 int 的子类，也在这里挡掉。
    """
    if isinstance(value, bool) or not isinstance(value, int | float):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def parse_metric_lines(text: str) -> list[dict[str, Any]]:
    """解析日志中的 ``POLARIS_METRIC {json}`` 行 → [{name, step, value}]。

    非有限值（NaN/Inf）跳过——它们进不了 JSONB，见 :func:`_is_storable_number`。
    """
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
        if not isinstance(name, str) or not _is_storable_number(value):
            continue
        step = data.get("step")
        points.append(
            {
                "name": name,
                "step": int(step) if _is_storable_number(step) else None,
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
        if _is_storable_number(value):
            points.append({"name": name, "step": None, "value": float(value)})
        elif isinstance(value, list):
            for item in value:
                if not isinstance(item, dict):
                    continue
                v = item.get("value")
                if not _is_storable_number(v):
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


def _evaluation_metric_lines(text: str) -> list[dict[str, Any]]:
    """Preserve invalid final observations as JSON-safe null diagnostic values.

    Dropping a final NaN would make a previous finite score look authoritative.
    Public parser helpers retain their historical filtering contract; controlled
    evaluations require this stricter ordered ingestion.
    """
    points = []
    for line in text.splitlines():
        match = re.search(r"\bPOLARIS_METRIC(?:\s+(.*)|$)", line)
        if not match:
            continue
        try:
            data = json.loads(match.group(1) or "")
        except json.JSONDecodeError:
            data = None
        if not isinstance(data, dict) or not isinstance(data.get("name"), str):
            points.append({"name": "__polaris_protocol_error__", "step": None, "value": None})
            continue
        value, step = data.get("value"), data.get("step")
        if data["name"] == "__polaris_protocol_error__":
            value = None
        points.append({"name": data["name"],
                       "step": int(step) if _is_storable_number(step) else None,
                       "value": float(value) if _is_storable_number(value) else None})
    return points


def _evaluation_metrics_json(text: str) -> list[dict[str, Any]]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        data = None
    if not isinstance(data, dict):
        return [{"name": "__polaris_protocol_error__", "step": None, "value": None}]
    points = []
    for name, series in data.items():
        if name == "__polaris_protocol_error__":
            points.append({"name": name, "step": None, "value": None})
            continue
        values = series if isinstance(series, list) else [{"value": series}]
        if not values:
            values = [{"value": None}]
        for item in values:
            item = item if isinstance(item, dict) else {"value": None}
            value, step = item.get("value"), item.get("step")
            points.append({"name": name,
                           "step": int(step) if _is_storable_number(step) else None,
                           "value": float(value) if _is_storable_number(value) else None})
    return points


def merge_metrics(target: dict[str, Any] | None, points: list[dict[str, Any]]) -> dict[str, Any]:
    """把指标点合并进 {name: [{step, value}]}（返回新 dict，便于 JSON 列写回）。"""
    merged: dict[str, Any] = {k: list(v) for k, v in (target or {}).items()}
    for point in points:
        merged.setdefault(point["name"], []).append(
            {"step": point["step"], "value": point["value"]}
        )
    return merged


def _metric_base(key: str) -> str:
    """指标键 → 归一化基名：剥掉 /条件/切片 后缀、统一小写。"""
    return key.split("/", 1)[0].strip().lower()


def extract_primary_value(metrics: dict[str, Any] | None, metric_name: str) -> float | None:
    """Only the frozen exact selector is comparable; ambiguous metric families are invalid."""
    return _last_value((metrics or {}).get(metric_name))


def is_improvement(value: float, best: float | None, direction: str) -> bool:
    """direction 感知比较：maximize 越大越好，minimize 越小越好。"""
    if best is None:
        return True
    return value > best if direction == "maximize" else value < best


# 同一错误签名连续出现 2 次起强制换根本策略；两次修复都没有产生
# 可验证进展时，统一恢复策略会在第 3 次失败后转向用户。
# 这不是修复次数上限（错误在变 = 有进展 = 一直修下去），是**零进展检测**——
# 线上实测 import 类错误秒级失败，无界修复循环一小时烧了 178 次 LLM 调用，
# 却始终在对同一个 ABI 冲突微调版本号。
_SIGNATURE_ESCALATE_AT = 2


# 签名实现提到共享模块（引擎重规划计数同用）；保留旧名给既有调用点/测试
_error_signature = error_signature


def _err_tail_line(err_text: str) -> str:
    """报错文本 → 给用户看的尾部关键行（原文，非归一化签名）。"""
    lines = [ln.strip() for ln in (err_text or "").strip().splitlines() if ln.strip()]
    return (lines[-1] if lines else "")[:200]


def _render_fix_ledger(ledger: list[dict[str, Any]]) -> str:
    """修复台账 → prompt 注入段（空台账返回空串）。"""
    if not ledger:
        return ""
    lines = ["此前的修复台账（同签名 = 那次修复没起作用）："]
    for i, entry in enumerate(ledger[-8:], 1):
        lines.append(
            f"{i}. 错误签名：{entry.get('signature')}；改动文件：{entry.get('changed') or '（无）'}"
        )
    return "\n".join(lines) + "\n"


def _phase_deadline_exceeded(budget: dict[str, Any] | None, phase_started: datetime) -> bool:
    """修复循环的唯一刹车：本阶段耗时超出 budget.max_hours（0/缺省 = 无限时）。

    用户定调：自动修复不按次数设限，只受一开始定义的时间预算约束。
    相位起点取循环开始（不含排队/等审批/等回答的时间）。"""
    max_hours = float((budget or {}).get("max_hours") or 0)
    return bool(max_hours) and _elapsed_hours(phase_started) > max_hours


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
    return float(v) if _is_storable_number(v) else None


def _conditions_delta(experiment: Experiment) -> dict[str, Any] | None:
    """对照实验的确定性汇总：按 plan.conditions 把 experiment.metrics 里各指标末值归到
    对应 condition（指标名以 /<condition> 结尾即归属，只聚合主指标族），算每组均值与相对
    baseline 的 delta。无 conditions 或无可归属指标时返回 None（退化为原单指标分析）。"""
    if not (experiment.plan or {}).get("conditions"):
        return None
    return evidence_service.condition_summary(
        experiment.metrics or {}, evidence_service.freeze_research_contract(experiment.plan or {})
    )


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
            library_ids = await get_source_library_ids(session, experiment.project_id)
            member_rows = (
                dedupe_member_rows(
                    (
                        await session.execute(
                            member_papers_stmt(library_ids)
                            .join(PaperWiki, PaperWiki.paper_id == Paper.id)
                            .where(LibraryPaper.status.in_(("compiled", "included")))
                        )
                    ).all()
                )
                if library_ids
                else []
            )
            member_rows.sort(
                key=lambda pm: (
                    -(pm[1].relevance_score if pm[1].relevance_score is not None else -1e18),
                    pm[1].created_at,
                )
            )
            rows = [(p.title, p.wiki_content) for p, _ in member_rows[:_WIKI_CONTEXT_PAPERS]]
            wiki_context = (
                "\n\n".join(
                    f"### {title}\n{(wiki or '')[:_WIKI_EXCERPT_CHARS]}" for title, wiki in rows
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
                f"{_guidance_line(params)}"
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
            await _commit_action(session, ctx)
            pm_def = plan.get("primary_metric") or {}
            hyp_texts = [str(h.get("text", ""))[:80] for h in plan.get("hypotheses", [])]
            _remember(
                ctx,
                "实验计划定稿",
                f"主指标：{pm_def.get('name')}（{pm_def.get('direction')}）；"
                f"假设 {len(hyp_texts)} 条：" + "；".join(hyp_texts[:6]) + "；"
                f"预算：{json.dumps(experiment.budget or {}, ensure_ascii=False)}",
            )
        plan = experiment.plan
        state = _iteration_state(experiment)
        if not state.get("research_contract"):
            state["research_contract"] = evidence_service.freeze_research_contract(
                plan, objective=ctx.run.goal, budget=experiment.budget,
                objective_details={"idea_id": str(idea.id), "title": idea.title,
                                   "summary": idea.summary, "research_type": idea.research_type,
                                   "goal": copy.deepcopy(idea.goal)},
            )
            experiment.iteration_state = state
            await _commit_action(session, ctx)
        ctx.checkpoint["research_contract"] = copy.deepcopy(state["research_contract"])

        # 预算闸门默认不拦（#626）：显式 confirm_budget=True 才预置闸门 payload 并把
        # 实验转 awaiting_gate；默认直接放行，下一步 setup 会把状态推进到 setup
        if _params(ctx).get("confirm_budget"):
            # 闸门 payload（engine 建 Gate 时合并）：实验 id + 预算摘要 + 计划摘要
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
            # 显式开了预算确认：固定管线下一站是 compute_budget 闸门
            await _set_status(ctx, session, experiment, "awaiting_gate")

    return {
        "hypotheses": len(plan.get("hypotheses", [])),
        "steps": len(plan.get("steps", [])),
        "primary_metric": plan.get("primary_metric"),
        "budget_estimate": plan.get("budget_estimate"),
    }


# ---- 2. 建环境（闸门后）：mkdir → LLM 代码生成 → 写文件 → venv ----


def _summarize_model_config(config_text: str) -> dict[str, Any]:
    """从模型 config.json 提取**中性事实**（不下兼容性判断）：model_type / architectures /
    有哪些配置分节（`*_config`）。是否合用（多模态、架构不被框架支持等）交给失败时的诊断 LLM——
    判断性任务不硬编码特判，预检只把事实摆出来（面板可见 + 供诊断消费）。"""
    try:
        cfg = json.loads(config_text)
    except (ValueError, TypeError):
        return {}
    if not isinstance(cfg, dict):
        return {}
    facts: dict[str, Any] = {}
    if isinstance(cfg.get("model_type"), str):
        facts["model_type"] = cfg["model_type"]
    archs = cfg.get("architectures")
    if isinstance(archs, list):
        arch_names = [str(a) for a in archs if isinstance(a, str)]
        if arch_names:
            facts["architectures"] = arch_names
    sections = sorted(k for k in cfg if k.endswith("_config") and isinstance(cfg.get(k), dict))
    if sections:
        facts["config_sections"] = sections
    return facts


def _host_path_for(ref: str, plan: dict[str, Any]) -> str:
    """容器内路径 → 宿主机路径（按 plan.container.mounts 反向映射）。

    预检在宿主机上探测，而容器计划声明的是容器内路径（如挂载 ~/hf→/hf 后的
    /hf/model/...）——不映射就必然误报「资源不存在」（线上实测，白打断用户一次）。
    非容器计划或无匹配挂载时原样返回。"""
    container = plan.get("container") if isinstance(plan.get("container"), dict) else None
    mounts = container.get("mounts") if container else None
    if not isinstance(mounts, dict):
        return ref
    for host_path, ctr_path in mounts.items():
        ctr_root = str(ctr_path).split(":", 1)[0]  # "/hf:ro" → "/hf"
        if ctr_root and (ref == ctr_root or ref.startswith(ctr_root.rstrip("/") + "/")):
            return str(host_path) + ref[len(ctr_root) :]
    return ref


async def _probe_resources(executor: Runner, plan: dict[str, Any]) -> tuple[list[dict], list[str]]:
    """资源预检（通用，不针对具体失败模式）：探 plan 声明的模型/数据集，把**事实**记进 resources
    （本机模型的 model_type/架构/配置分节、存在性），只对**普适**问题告警（声明的本机资源不存在）。
    ref 以 ~ 或 / 开头 = 本机路径（容器内路径先经挂载表反向映射）；否则视为 HF id
    （会下载，跳过）。探测异常不冒泡，不崩 setup。"""
    resources: list[dict] = []
    warnings: list[str] = []
    for m in plan.get("models") or []:
        ref = (m.get("ref") if isinstance(m, dict) else str(m)) or ""
        if not ref:
            continue
        role = m.get("role") if isinstance(m, dict) else ""
        entry: dict[str, Any] = {"kind": "model", "ref": ref, "role": role}
        if ref.startswith(("~", "/")):  # 本机模型
            host_ref = _host_path_for(ref, plan)
            try:
                cfg = await executor.read_host_file(f"{host_ref}/config.json")
            except Exception:  # noqa: BLE001 — 预检探测失败不阻断 setup
                cfg = None
            entry["found"] = cfg is not None
            if cfg is None:
                warnings.append(
                    f"资源预检告警：声明的本机模型 {ref} 不存在（找不到 config.json）。"
                )
            else:
                facts = _summarize_model_config(cfg)
                if facts:  # 中性事实（model_type/architectures/config_sections），不下判断
                    entry["config"] = facts
        else:
            entry["remote"] = True  # HF id：会下载，跳过本机存在性
        resources.append(entry)
    for d in plan.get("datasets") or []:
        name = (d.get("name") if isinstance(d, dict) else str(d)) or ""
        if name and str(name).startswith(("~", "/")):  # 只查本机路径数据集；HF 名会下载
            try:
                exists = await executor.host_path_exists(_host_path_for(str(name), plan))
            except Exception:  # noqa: BLE001
                continue
            resources.append({"kind": "dataset", "ref": name, "found": exists})
            if not exists:
                warnings.append(f"资源预检告警：声明的本机数据集 {name} 不存在。")
    return resources, warnings


# 排队等待 runner 主机席位的上限（秒）：给在跑实验一点收尾腾位的余地，又不让
# setup 无限悬挂——超时转为可诊断失败，由引擎失败分派决定重试/换方案/问人。
RESOURCE_LEASE_WAIT_SECONDS = 600.0


async def _acquire_resource_lease(ctx: ActionContext) -> None:
    """备环境前获取 runner 主机租约（#716 接线 #677/#685）。

    checkpoint.params.resource_id 非空 = 用户创建实验时指定了注册的 runner 主机；
    在 prepare 语义（experiment_setup 连接主机之前）排队拿席位，独占主机上两个
    实验不再同时开跑。释放不在这里管：voyage run 的四个终态写入点都挂了
    release_for_run 兜底（engine._set_status / cancel_voyage / gates.fail_voyage /
    api.voyages 删除路径），不管实验怎么死都不占着资源。

    用独立 session：wait_and_acquire 内部会 commit/rollback，混用调用方 session
    会把人家已加载的 ORM 对象状态搅乱。
    """
    raw = _params(ctx).get("resource_id")
    if not raw:
        return
    async with get_sessionmaker()() as session:
        resource = await session.get(Resource, uuid.UUID(str(raw)))
        if resource is None:
            # 资源被删——转可诊断失败（引擎分派），不是崩溃
            raise ValueError(f"指定的 runner 主机资源已不存在：{raw}")
        # 幂等：setup 失败重试/断点续跑会重入本函数，run 已持有该资源的活租约就不叠加
        held = await session.execute(
            select(ResourceLease.id).where(
                ResourceLease.resource_id == resource.id,
                ResourceLease.run_id == ctx.run.id,
                ResourceLease.released_at.is_(None),
            )
        )
        if held.first() is not None:
            return
        try:
            await resource_leases_service.wait_and_acquire(
                session,
                resource,
                ctx.run,
                timeout=RESOURCE_LEASE_WAIT_SECONDS,
                note="experiment.setup",
            )
        except resource_leases_service.ResourceBusyError as e:
            raise ValueError(
                f"runner 主机忙：等待 {RESOURCE_LEASE_WAIT_SECONDS:.0f} 秒仍没有空闲席位（{e}）。"
                "可稍后重试，或换一台注册主机。"
            ) from e
    await ctx.log("已获得 runner 主机资源席位")


@register("experiment.setup")
@_guarded
async def experiment_setup(ctx: ActionContext, params: dict[str, Any]) -> dict[str, Any]:
    async with get_sessionmaker()() as session:
        experiment = await _get_experiment(session, ctx)
        await _set_status(ctx, session, experiment, "setup")

        # 资源租约（#716）：指定了 runner 主机资源时，备环境前先排队拿席位；
        # 拿不到转为可诊断失败（引擎失败分派处理），终态释放由 release_for_run 兜底
        await _acquire_resource_lease(ctx)

        # 实验的全局环境设置（管理端「实验设置」里配）：模型/数据集位置、pip 镜像、
        # HF 端点、代理。既写进 env.sh，也**作为事实写进 codegen 提示词**——模型不知道
        # 这台机器上模型放在哪，只能照提示词里的例子猜。实测一次失败（voyage 6c5df454）
        # 生成了 /hf/Qwen/Qwen3-1.7B，少了一层目录，冒烟直接起不来。
        env_settings = await experiment_settings_service.get_settings(session)

        files = ctx.checkpoint.get("exp_files")
        _remember_guidance(ctx, params)
        if not isinstance(files, dict):  # 断点幂等：已生成的代码不重复调 LLM
            user_prompt = (
                f"实验计划：{json.dumps(experiment.plan or {}, ensure_ascii=False)[:8000]}\n"
                f"{_memory_prompt(ctx)}"
                f"{_guidance_line(params)}"
                f"预算：{json.dumps(experiment.budget or {}, ensure_ascii=False)}"
                f"{_env_facts_prompt(env_settings)}"
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
        # 代理优先用凭据上配的（那是「这台机器」的属性），没配才回落到全局实验设置
        proxy_url = executor.proxy_url or env_settings.get("proxy_url") or None
        platform_files = (
            _platform_env_files(
                ctx,
                proxy_url=proxy_url,
                no_proxy_extra=llm_host,
                env_settings=env_settings,
            )
            | eval_files
        )
        try:
            await executor.mkdir_workdir()
            experiment.workdir = executor.workdir
            experiment.server_host = executor.host
            await _commit_action(session, ctx)
            await executor.write_files(platform_files)  # 平台文件写一次（不随修复变）

            # 资源预检（GPU + 模型/数据集）：确定性探测，记进观测（面板可见），有问题给早期告警。
            # **只告警不硬停**——资源问题硬停会触发 setup 换方案重规划（抹掉失败步骤、丢诊断）；
            # 真正的拦截（换机闸门）留后续，这刀先把资源可见性 + 早期告警做扎实。
            plan = experiment.plan if isinstance(experiment.plan, dict) else {}
            container = plan.get("container") if isinstance(plan.get("container"), dict) else None
            gpus = await executor.probe_gpu()
            resources, preflight_warnings = await _probe_resources(executor, plan)
            preflight_warnings = list(dict.fromkeys(preflight_warnings))  # 去重（曾三连重复）
            needs_gpu = plan.get("kind") == "training" or bool(container and container.get("gpus"))
            if needs_gpu and not gpus:
                preflight_warnings.append(
                    f"资源预检告警：{executor.host} 上探测不到可用 GPU（nvidia-smi 无输出），"
                    "但这是训练类/声明了 GPU 的实验——如后续因显存/设备失败，"
                    "请换一台有空闲 GPU 的服务器，或把方案改成不需要 GPU 的实现。"
                )

            # 资源硬缺失（声明的本机模型/数据集不存在）不再只是告警：以前预检连告
            # 三次没人消费、流程照走，直到冒烟才炸。现在转向用户提问（只问一次，
            # 答案经建议通道注入后续 codegen；用户也可选择硬跑）。
            fatal_missing = [w for w in preflight_warnings if "不存在" in w]
            if fatal_missing and not ctx.checkpoint.get("setup_resource_asked"):
                ctx.checkpoint["setup_resource_asked"] = True
                _remember(ctx, "资源缺口", "；".join(fatal_missing)[:400] + "，已转向用户提问")
                await _sync_memory_file(ctx, executor)
                return {
                    "workdir": experiment.workdir,
                    "preflight_warnings": preflight_warnings,
                    "ask": {
                        "ask_kind": "fatal_step",
                        "question": (
                            "预检发现声明的本机资源不存在："
                            + "；".join(w[:120] for w in fatal_missing[:3])
                            + " 请给出正确路径，或选择改为在线下载/硬跑。"
                        ),
                        "context": {"preflight_warnings": preflight_warnings},
                        "options": [
                            {
                                "id": "provide_path",
                                "zh": "我来给正确路径（在输入框写明）",
                                "en": "I'll provide the correct path",
                            },
                            {
                                "id": "use_hf",
                                "zh": "改为在线下载（HF）",
                                "en": "Download from HF instead",
                            },
                            {"id": "abort", "zh": "放弃实验", "en": "Give up"},
                        ],
                    },
                }

            # 依赖安装自愈（对称 smoke）：装不上/太慢/断连都当「可修的失败」而非硬崩——
            # 超时/断连→重连重试；pip 报错→回 LLM 修 requirements.txt/run.sh 再装。
            # 修复次数不设上限（用户定调：只受时间预算约束，默认无限时）；
            # 每轮修复都是一次真实安装（分钟级），LLM 调用频率天然被实际工作限速。
            fixes = 0
            attempts = 0
            phase_started = utcnow()
            last_signature = ""
            sig_streak = 0
            fix_ledger: list[dict[str, Any]] = []
            resumed_handle = _restore_managed_handle(
                ctx.checkpoint.pop("managed_command_waiting", None)
            )
            if resumed_handle:
                # Reconcile the original operation before preparing a replacement container.
                prepare_handle = (
                    resumed_handle if resumed_handle.operation_id == "environment-prepare" else None
                )
            else:
                launch_attempt = await _reserve_managed_launch(
                    ctx, session, executor, experiment, phase="environment.prepare",
                    operation="environment-prepare", command="prepare experiment environment",
                )
                if launch_attempt is None:
                    return {"cancelled": True}
                prepare_handle = await executor.prepare_managed(attempt_id=launch_attempt)
                if prepare_handle is None:
                    state = _iteration_state(experiment)
                    state.pop("remote_cleanup", None)  # This preparation launched no operation.
                    experiment.iteration_state = state
                    await _commit_action(session, ctx)
            if prepare_handle is not None:
                try:
                    prepare_snapshot, executor = await _monitor_managed_command(
                        ctx, session, executor, experiment, prepare_handle
                    )
                except ManagedCommandNeedsUser as pending:
                    return _managed_command_waiting_result(ctx, experiment, pending)
                except ManagedCommandCancelled as cancelled:
                    executor = cancelled.executor
                    return {"cancelled": True}
                if prepare_snapshot.exit_status != 0:
                    report = failure_from_snapshot(prepare_snapshot)
                    _plan, next_step = await _plan_failure_recovery(ctx, report)
                    return {
                        "workdir": experiment.workdir,
                        "failure": report.to_dict(),
                        "ask": {
                            "ask_kind": "command_recovery",
                            "question": (
                                f"远端环境准备失败：{report.message[-500:]}\n\n建议：{next_step}"
                            ),
                            "context": {"failure": report.to_dict(), "next_step": next_step},
                            "options": [
                                {"id": "retry", "zh": "重试环境准备", "en": "Retry setup"},
                                {"id": "replan", "zh": "更换运行方案", "en": "Change plan"},
                                {"id": "abort", "zh": "放弃实验", "en": "Give up"},
                            ],
                        },
                    }
                resumed_handle = None
            while True:
                attempts += 1
                await executor.write_files(files)  # 每次（修复后）重写 LLM 产出文件
                hint = ""
                failure_report: FailureReport | None = None
                try:
                    launch_attempt = None
                    if resumed_handle is None:
                        launch_attempt = await _reserve_managed_launch(
                            ctx, session, executor, experiment, phase="dependency.install",
                            operation="dependency-install",
                            command="install experiment dependencies",
                        )
                        if launch_attempt is None:
                            return {"cancelled": True}
                    handle = resumed_handle or await executor.launch_managed_setup(
                        attempt_id=launch_attempt
                    )
                    resumed_handle = None
                    snapshot, executor = await _monitor_managed_command(
                        ctx, session, executor, experiment, handle
                    )
                    exit_status = int(snapshot.exit_status or 0)
                    err_text = snapshot.stderr_tail or snapshot.stdout_tail
                    if exit_status != 0:
                        failure_report = failure_from_snapshot(snapshot)
                except ManagedCommandNeedsUser as pending:
                    return _managed_command_waiting_result(ctx, experiment, pending)
                except ManagedCommandCancelled as cancelled:
                    executor = cancelled.executor
                    return {"cancelled": True}
                if exit_status == 0:
                    env_bits = [f"探测到 GPU {len(gpus)} 卡" if gpus else "未探测到 GPU"]
                    for warning in preflight_warnings:
                        env_bits.append(str(warning)[:200])
                    if fixes:
                        env_bits.append(f"依赖安装经 {fixes} 次自动修复后通过")
                    _remember(ctx, "环境事实", "；".join(env_bits))
                    await _sync_memory_file(ctx, executor)
                    written = list(files) + list(platform_files)
                    obs: dict[str, Any] = {
                        "workdir": experiment.workdir,
                        "files": written,
                        "venv_exit": 0,
                        "attempts": attempts,
                        "fixes": fixes,
                        "gpus": gpus,  # 资源预检探到的 GPU（供面板/后续显存决策）
                        "resources": resources,  # 模型/数据集探测结果（存在性/多模态）
                    }
                    if preflight_warnings:
                        obs["preflight_warnings"] = preflight_warnings
                    return obs
                if _phase_deadline_exceeded(experiment.budget, phase_started):
                    # 不按修复次数设限（用户定调），只受时间预算约束：超时转向用户提问。
                    # 抛错会让引擎原地重试整个 setup——又从头拉镜像/装依赖一遍
                    # （线上实测「一直装镜像」就是这个循环），所以一律提问不抛错。
                    detail = err_text or "（无输出，多为连接中断或超时）"
                    await _mark_attention(
                        ctx, f"依赖安装超出时间预算（{attempts} 次，exit={exit_status}）"
                    )
                    _remember(
                        ctx,
                        "环境障碍",
                        f"依赖安装超出时间预算（{attempts} 次尝试，exit={exit_status}）："
                        f"{detail[-200:]}，已转向用户提问",
                    )
                    await _sync_memory_file(ctx, executor)
                    return {
                        "workdir": experiment.workdir,
                        "venv_exit": exit_status,
                        "attempts": attempts,
                        "fixes": fixes,
                        "ask": {
                            "ask_kind": "fatal_step",
                            "question": (
                                f"实验环境装不起来（已尝试 {attempts} 次、"
                                f"自动修复 {fixes} 次，超出时间预算），"
                                f"最后的报错：{detail[-300:]}。怎么处理？"
                            ),
                            "context": {
                                "setup_log_tail": detail[-2000:],
                                "attempts": attempts,
                                "fixes": fixes,
                                "preflight_warnings": preflight_warnings,
                            },
                            "options": [
                                {
                                    "id": "retry",
                                    "zh": "带指示重试（换依赖/换镜像/改配置等）",
                                    "en": "Retry with instructions",
                                },
                                {
                                    "id": "replan",
                                    "zh": "换个方案（请说明思路）",
                                    "en": "Change approach (describe how)",
                                },
                                {"id": "abort", "zh": "放弃实验", "en": "Give up"},
                            ],
                        },
                    }
                # 零进展检测（不是次数上限）：同签名错误在原地打转 → 先升级策略，
                # 仍打转 → 转向用户提问。import 类错误秒级失败，没有这个刹车时
                # 无界修复循环会变成高频 token 空转（线上实测一小时 178 次 LLM 调用）。
                signature = _error_signature(err_text)
                sig_streak = sig_streak + 1 if signature == last_signature else 1
                last_signature = signature
                if failure_report is None:
                    raise RuntimeError("managed dependency command failed without a failure report")
                recovery_plan, next_step = await _plan_failure_recovery(ctx, failure_report)
                repeated_without_progress = sig_streak - 1
                if recovery_plan is None or not may_apply_recovery_automatically(
                    recovery_plan,
                    repeated_without_progress=repeated_without_progress,
                ):
                    repeated = repeated_without_progress >= 2
                    return {
                        "workdir": experiment.workdir,
                        "venv_exit": exit_status,
                        "attempts": attempts,
                        "fixes": fixes,
                        "failure": failure_report.to_dict(),
                        "recovery_plan": {
                            "diagnosis": recovery_plan.diagnosis,
                            "confidence": recovery_plan.confidence,
                            "repair_scope": recovery_plan.repair_scope,
                            "proposed_changes": recovery_plan.proposed_changes,
                            "expected_evidence": recovery_plan.expected_evidence,
                            "minimal_retry": recovery_plan.minimal_retry,
                        }
                        if recovery_plan
                        else None,
                        "ask": {
                            "ask_kind": "command_recovery",
                            "question": (
                                f"依赖安装反复失败在同一个错误上（连续 {sig_streak} 次）："
                                f"{_err_tail_line(err_text)}。自动修复没有取得可验证的进展，"
                                "远端命令已结束，请决定下一步。"
                                if repeated
                                else (
                                    f"远端命令失败：{failure_report.message[-500:]}\n\n"
                                    f"建议：{next_step}"
                                )
                            ),
                            "context": {
                                "failure": failure_report.to_dict(),
                                "next_step": next_step,
                                "error_signature": signature,
                                "fix_ledger": fix_ledger[-8:],
                            },
                            "options": [
                                {
                                    "id": "retry",
                                    "zh": "按建议调整后重试",
                                    "en": "Apply the suggestion and retry",
                                },
                                {
                                    "id": "replan",
                                    "zh": "更换实验方案",
                                    "en": "Change the experiment plan",
                                },
                                {"id": "abort", "zh": "放弃实验", "en": "Give up"},
                            ],
                        },
                    }
                hint = (
                    f"模型诊断（置信度 {recovery_plan.confidence:.2f}）："
                    f"{recovery_plan.diagnosis}；预期证据：{recovery_plan.expected_evidence}"
                )
                # 把诊断 + 报错回给 LLM 修 requirements.txt/run.sh（方案级修复）。
                # 环境事实必须一起带上：修复循环原本只有 stderr，模型照样不知道这台机器
                # 上模型放哪、有没有配镜像源，只能接着猜。修复前先拉取对话流里
                # 新到的用户建议——装依赖动辄几十分钟，用户在旁边说话不能装听不见。
                fixes += 1
                hint = hint or diagnose_failure(err_text, env_settings)
                guidance_line = await _refresh_user_guidance(ctx, params)
                _remember_guidance(ctx, params)
                escalate_line = (
                    f"⚠️ 同一个错误已连续出现 {sig_streak} 次，之前的修复完全没起作用。"
                    "**禁止再微调版本号重试**——换根本不同的策略：卸载/固定冲突的包、"
                    "绕开该库、遵循预装栈的版本组合、或换完全不同的实现路径；"
                    "先一句话说明新策略为什么能避开这个错误。\n"
                    if sig_streak >= _SIGNATURE_ESCALATE_AT
                    else ""
                )
                user_prompt = (
                    f"当前文件：{json.dumps(files, ensure_ascii=False)[:8000]}\n\n"
                    + _memory_prompt(ctx)
                    + guidance_line
                    + escalate_line
                    + _render_fix_ledger(fix_ledger)
                    + f"依赖安装退出码：{exit_status}\n"
                    + (f"诊断提示：{hint}\n" if hint else "")
                    + f"报错：\n{err_text}"
                    + _env_facts_prompt(env_settings)
                )
                new_files = await _complete_json(
                    ctx,
                    system=_prompt_with_context(SETUP_FIX_SYSTEM_PROMPT, ctx),
                    user=user_prompt,
                    validate=validate_files,
                )
                changed = sorted(k for k in new_files if new_files.get(k) != files.get(k))
                fix_ledger.append({"signature": signature, "changed": ", ".join(changed)})
                files = new_files
                ctx.checkpoint["exp_files"] = files
        finally:
            await executor.close()


# ---- 3. 冒烟测试：exit 0 通过；失败回 LLM 修文件（不限次数，超时预算转提问） ----


@register("experiment.smoke")
@_guarded
async def experiment_smoke(ctx: ActionContext, params: dict[str, Any]) -> dict[str, Any]:
    async with get_sessionmaker()() as session:
        experiment = await _get_experiment(session, ctx)
        files: dict[str, str] = dict(ctx.checkpoint.get("exp_files") or {})
        # 修复循环也要拿到环境事实（模型/数据集位置、镜像源），否则它只能对着 stderr 猜
        env_settings = await experiment_settings_service.get_settings(session)

        executor = await _open_executor(session, ctx, experiment)
        try:
            attempts = 0
            fixes = 0
            phase_started = utcnow()  # 修复不限次数，只受时间预算约束（用户定调）
            last_signature = ""
            sig_streak = 0
            fix_ledger: list[dict[str, Any]] = []
            resumed_handle = _restore_managed_handle(
                ctx.checkpoint.pop("managed_command_waiting", None)
            )
            if resumed_handle and resumed_handle.operation_id != "application-smoke":
                # An interrupted dependency repair is not a passed smoke test.
                try:
                    _snapshot, executor = await _monitor_managed_command(
                        ctx, session, executor, experiment, resumed_handle
                    )
                except ManagedCommandNeedsUser as pending:
                    return _managed_command_waiting_result(ctx, experiment, pending)
                except ManagedCommandCancelled as cancelled:
                    executor = cancelled.executor
                    return {"cancelled": True}
                resumed_handle = None
            while True:
                attempts += 1
                # 超时/断连也当作「可修的失败」（多为规模太大/太慢或环境问题），而非硬崩：
                # 诊断为「太慢/超时」→ 让 LLM 把冒烟改小改快再试，正是自适应循环该自愈的一类。
                hint = ""
                failure_report: FailureReport | None = None
                try:
                    launch_attempt = None
                    if resumed_handle is None:
                        launch_attempt = await _reserve_managed_launch(
                            ctx, session, executor, experiment, phase="application.smoke",
                            operation="application-smoke", command="bash run.sh --smoke",
                        )
                        if launch_attempt is None:
                            return {"cancelled": True}
                    handle = resumed_handle or await executor.launch_managed_smoke(
                        attempt_id=launch_attempt
                    )
                    resumed_handle = None
                    snapshot, executor = await _monitor_managed_command(
                        ctx, session, executor, experiment, handle
                    )
                    exit_status = int(snapshot.exit_status or 0)
                    err_text = snapshot.stderr_tail or snapshot.stdout_tail
                    if exit_status != 0:
                        failure_report = failure_from_snapshot(snapshot)
                except ManagedCommandNeedsUser as pending:
                    return _managed_command_waiting_result(ctx, experiment, pending)
                except ManagedCommandCancelled as cancelled:
                    executor = cancelled.executor
                    return {"cancelled": True}
                if exit_status == 0:
                    if fixes:
                        _remember(ctx, "试跑", f"自动修复 {fixes} 次后通过")
                    await _sync_memory_file(ctx, executor)
                    return {"exit_code": 0, "attempts": attempts, "fixes": fixes}
                if _phase_deadline_exceeded(experiment.budget, phase_started):
                    # 修复不设次数上限（用户定调），超出时间预算才转向用户提问
                    # （引擎收到 observation.ask 会把节点回 pending、任务转 paused_ask）。
                    # 回答「重试」会从头重跑本动作，回答文本经 params["user_guidance"] 注入。
                    await _mark_attention(
                        ctx, f"冒烟测试超出时间预算（{attempts} 次，exit={exit_status}）"
                    )
                    _remember(
                        ctx,
                        "试跑障碍",
                        f"冒烟超出时间预算（{attempts} 次尝试，exit={exit_status}）："
                        f"{err_text[-200:]}，已转向用户提问",
                    )
                    await _sync_memory_file(ctx, executor)
                    return {
                        "exit_code": exit_status,
                        "attempts": attempts,
                        "fixes": fixes,
                        "ask": {
                            "ask_kind": "fatal_step",
                            "question": (
                                f"代码试跑一直不通过（已尝试 {attempts} 次、"
                                f"自动修复 {fixes} 次，超出时间预算），"
                                f"最后的报错：{err_text[-300:]}。怎么处理？"
                            ),
                            "context": {
                                "stderr_tail": err_text[-2000:],
                                "attempts": attempts,
                                "fixes": fixes,
                            },
                            "options": [
                                {
                                    "id": "retry",
                                    "zh": "带指示重试（换依赖/改配置等）",
                                    "en": "Retry with instructions",
                                },
                                {
                                    "id": "replan",
                                    "zh": "换个方案（请说明思路）",
                                    "en": "Change approach (describe how)",
                                },
                                {"id": "abort", "zh": "放弃实验", "en": "Give up"},
                            ],
                        },
                    }
                # 把诊断提示 + stderr 回给 LLM 修文件（方案级修复）。
                # 超时那类已经在上面给了 hint；其余按 stderr 签名归类，认不出就留空。
                # 零进展检测（同 setup）：同签名连发先升级策略、再转提问
                signature = _error_signature(err_text)
                sig_streak = sig_streak + 1 if signature == last_signature else 1
                last_signature = signature
                if failure_report is None:
                    raise RuntimeError("managed smoke command failed without a failure report")
                recovery_plan, next_step = await _plan_failure_recovery(ctx, failure_report)
                repeated_without_progress = sig_streak - 1
                if recovery_plan is None or not may_apply_recovery_automatically(
                    recovery_plan,
                    repeated_without_progress=repeated_without_progress,
                ):
                    repeated = repeated_without_progress >= 2
                    return {
                        "exit_code": exit_status,
                        "attempts": attempts,
                        "fixes": fixes,
                        "failure": failure_report.to_dict(),
                        "ask": {
                            "ask_kind": "command_recovery",
                            "question": (
                                f"代码试跑反复失败在同一个错误上（连续 {sig_streak} 次）："
                                f"{_err_tail_line(err_text)}。自动修复没有取得可验证的进展，"
                                "远端命令已结束，请决定下一步。"
                                if repeated
                                else (
                                    f"冒烟命令失败：{failure_report.message[-500:]}\n\n"
                                    f"建议：{next_step}"
                                )
                            ),
                            "context": {
                                "failure": failure_report.to_dict(),
                                "next_step": next_step,
                                "error_signature": signature,
                                "fix_ledger": fix_ledger[-8:],
                            },
                            "options": [
                                {"id": "retry", "zh": "按建议修复并重试", "en": "Repair and retry"},
                                {"id": "replan", "zh": "更换实验方案", "en": "Change plan"},
                                {"id": "abort", "zh": "放弃实验", "en": "Give up"},
                            ],
                        },
                    }
                hint = (
                    f"模型诊断（置信度 {recovery_plan.confidence:.2f}）："
                    f"{recovery_plan.diagnosis}；预期证据：{recovery_plan.expected_evidence}"
                )
                fixes += 1
                hint = hint or diagnose_failure(err_text, env_settings)
                # 修复前拉取对话流里新到的用户建议（试跑修复同样是长循环）
                guidance_line = await _refresh_user_guidance(ctx, params)
                _remember_guidance(ctx, params)
                escalate_line = (
                    f"⚠️ 同一个错误已连续出现 {sig_streak} 次，之前的修复完全没起作用。"
                    "**禁止再微调版本号重试**——换根本不同的策略：卸载/固定冲突的包、"
                    "绕开该库、遵循预装栈的版本组合、或换完全不同的实现路径；"
                    "先一句话说明新策略为什么能避开这个错误。\n"
                    if sig_streak >= _SIGNATURE_ESCALATE_AT
                    else ""
                )
                user_prompt = (
                    f"当前文件：{json.dumps(files, ensure_ascii=False)[:8000]}\n\n"
                    + _memory_prompt(ctx)
                    + guidance_line
                    + escalate_line
                    + _render_fix_ledger(fix_ledger)
                    + f"冒烟测试退出码：{exit_status}\n"
                    + (f"诊断提示：{hint}\n" if hint else "")
                    + f"stderr：\n{err_text[-_STDERR_CHARS:]}"
                    + _env_facts_prompt(env_settings)
                )
                new_files = await _complete_json(
                    ctx,
                    system=_prompt_with_context(FIX_SYSTEM_PROMPT, ctx),
                    user=user_prompt,
                    validate=validate_files,
                )
                changed = sorted(k for k in new_files if new_files.get(k) != files.get(k))
                fix_ledger.append({"signature": signature, "changed": ", ".join(changed)})
                files = new_files
                ctx.checkpoint["exp_files"] = files
                await executor.write_files(files)
                if "requirements.txt" in changed:
                    # 依赖清单变了必须重装，否则修复根本不落地——线上实测：修复循环
                    # 连续三轮都正确地在 requirements.txt 里升级 transformers，但依赖
                    # 只在 setup 步装过一次，环境纹丝不动、同签名三连触发零进展提问。
                    # 模型做对了，平台把它的修复扔了。安装失败不在这里打断：下一轮
                    # 试跑的 stderr 会带出真实症状，交回修复循环。
                    await ctx.log("requirements.txt 有变更，重装依赖后再试跑")
                    dep, executor, waiting = await _run_managed_phase(
                        ctx, session, executor, experiment, phase="dependency.install",
                        operation="dependency-install", command="install experiment dependencies",
                        launch_method="launch_managed_setup",
                    )
                    if waiting is not None:
                        return waiting
                    if dep.exit_status != 0:
                        await ctx.log(
                            f"依赖重装未成功（exit={dep.exit_status}），试跑将带错重修",
                            level="warn",
                        )
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
    state = copy.deepcopy(experiment.iteration_state or {})
    return {
        **state,
        "no_improve_streak": int(state.get("no_improve_streak") or 0),
        "debug_count": int(state.get("debug_count") or 0),
        "stopped_reason": state.get("stopped_reason"),
    }


def _best_primary_value(runs: list[ExperimentRun], direction: str) -> float | None:
    values = [r.primary_value for r in runs
              if r.status == "succeeded" and r.exit_code == 0
              and _is_storable_number(r.primary_value)]
    if not values:
        return None
    return max(values) if direction == "maximize" else min(values)


@register("experiment.run")
@_guarded
async def experiment_run(ctx: ActionContext, params: dict[str, Any]) -> dict[str, Any]:
    """Persist source/identity before launch, reconcile attempts, then validate and promote."""
    async with get_sessionmaker()() as session:
        experiment = await _get_experiment(session, ctx)
        await _set_status(ctx, session, experiment, "running")
        state = _iteration_state(experiment)
        contract = state.get("research_contract") or evidence_service.freeze_research_contract(
            experiment.plan or {}, objective=ctx.run.goal, budget=experiment.budget
        )
        if not state.get("candidates") and (experiment.plan or {}).get("protected_files"):
            contract = evidence_service.freeze_research_contract(
                experiment.plan or {}, objective=ctx.run.goal, budget=experiment.budget,
                initial_files=ctx.checkpoint.get("exp_files") or {},
                objective_details=contract.get("objective_details"),
            )
        state["research_contract"] = contract
        ctx.checkpoint["research_contract"] = copy.deepcopy(contract)
        pm = contract["protocol"]["primary_metric"]
        budget = experiment.budget or {}
        max_hours = float(budget.get("max_hours") or 0)
        max_runs = int(budget.get("max_runs") or 0)
        iterate_cp = dict(ctx.checkpoint.get("iterate") or {})
        iterate_cp.setdefault("started_at", state.get("iterate_started_at")
                              or state.get("execution_started_at") or utcnow().isoformat())
        state["iterate_started_at"] = iterate_cp["started_at"]
        ctx.checkpoint["iterate"] = iterate_cp
        iterate_started = datetime.fromisoformat(str(iterate_cp["started_at"]))
        runs = list((await session.execute(
            select(ExperimentRun).where(ExperimentRun.experiment_id == experiment.id)
            .order_by(ExperimentRun.seq)
        )).scalars().all())
        metadata = dict(state.get("run_metadata") or {})
        step_id = str(ctx.step_id) if ctx.step_id else None
        previous = next((r for r in runs if metadata.get(str(r.id), {}).get("step_id") == step_id),
                        None) if step_id else None
        if previous and previous.status != "running":
            evaluation = next((e for e in state.get("evaluations", [])
                               if e["run_id"] == str(previous.id)), None)
            if evaluation is not None:
                iterate_cp["primary_metric_runs"] = sum(
                    bool(e.get("valid")) for e in state.get("evaluations", [])
                )
                return {"run_id": str(previous.id), "seq": previous.seq,
                        "exit_code": previous.exit_code, "run_status": previous.status,
                        "primary_value": previous.primary_value,
                        "evaluation_valid": evaluation["valid"]}
        stale = previous or (runs[-1] if runs and runs[-1].status == "running" else None)
        seq = stale.seq if stale else (runs[-1].seq + 1 if runs else 1)
        # An exhausted budget can forbid a NEW launch; an old attempt must be reconciled first.
        if stale is None:
            reason = ("max_runs" if max_runs and seq > max_runs else
                      "max_hours" if max_hours and _elapsed_hours(iterate_started) >= max_hours
                      else None)
            if reason:
                state["stopped_reason"] = reason
                experiment.iteration_state = state
                await _commit_action(session, ctx)
                iterate_cp["stopped_reason"] = reason
                return {"skipped": True, "stopped_reason": reason,
                        "plan_signal": {"decision": "finish", "stopped_reason": reason}}
        executor = await _open_executor(session, ctx, experiment)

        async def launch_with_budget() -> ManagedCommandHandle | None:
            if not await _commit_action(session, ctx, publication=True):
                raise ManagedLaunchCancelled()
            remaining = (max_hours * 3600 - _elapsed_hours(iterate_started) * 3600
                         if max_hours else None)
            if remaining is not None and remaining <= 0:
                return None
            return await executor.launch_managed_run(timeout_seconds=remaining)

        try:
            if stale is not None:
                run = stale
                meta = metadata.get(str(run.id), {})
                if meta.get("workspace_id"):
                    executor.bind_run_workspace(meta["workspace_id"])
                candidate = next((c for c in state.get("candidates", [])
                                  if c["candidate_id"] == meta.get("candidate_id")), None)
                # Replay is scoped to this run; aggregate metrics are rebuilt after validation.
                run.metrics = None
                recovered = await executor.recover_managed_command(OperationContext(
                    phase="application.run", operation="experiment-run",
                    display_command=run.command, target=experiment.server_host,
                    soft_timeout_seconds=600, stall_timeout_seconds=900,
                    hard_timeout_seconds=max_hours * 3600 if max_hours else None,
                    repair_scope=RepairScope.APPLICATION_FILES,
                ))
                managed_handle = recovered if recovered and (
                    run.pid is None or recovered.process_id == run.pid
                ) else None
                if managed_handle is not None:
                    run.pid = managed_handle.process_id
                elif run.pid is None:
                    if candidate is None:
                        raise ValueError("reserved run has no authoritative candidate snapshot")
                    if not meta.get("workspace_prepared"):
                        old_workspace = meta.get("workspace_id")
                        workspace_id = str(uuid.uuid4())
                        await executor.prepare_run_workspace(workspace_id, candidate["files"])
                        meta["abandoned_workspace_ids"] = [
                            *(meta.get("abandoned_workspace_ids") or []), old_workspace,
                        ]
                        meta["workspace_id"] = workspace_id
                        meta["workspace_prepared"] = True
                        state["run_metadata"] = metadata
                        experiment.iteration_state = state
                        await _commit_action(session, ctx)
                    elif await executor.list_dir(".polaris/operations/experiment-run/attempts"):
                        return {"run_id": str(run.id), "remote_operation_continues": True,
                                "ask": {"ask_kind": "action_ask", "question":
                                        "远端启动记录存在，但进程身份尚不能恢复；请等待对账或停止该任务。"}}
                    else:
                        for name, source in candidate["files"].items():
                            if (await executor.read_file(name)).decode("utf-8") != source:
                                raise ValueError("prepared run source changed before launch")
                    managed_handle = await launch_with_budget()
                    if managed_handle is not None:
                        run.pid = managed_handle.process_id
                elif meta.get("workspace_id"):
                    return {"run_id": str(run.id), "remote_operation_continues": True,
                            "ask": {"ask_kind": "action_ask", "question":
                                    "当前运行的持久化进程身份无法核对，保留资源并等待远端对账。"}}
                if not run.log_path:
                    run.log_path = str(experiments_service.append_local_log(
                        experiment.id, seq, ""
                    ))
            else:
                files = validate_files({"files": ctx.checkpoint.get("exp_files") or {}})
                protected = contract["protocol"].get("protected_files") or []
                protected_sources = dict(state.get("protected_sources") or {})
                if not protected_sources:
                    protected_sources = {name: files[name] for name in protected if name in files}
                    if len(protected_sources) != len(protected):
                        raise ValueError("protected evaluator file missing from baseline")
                    state["protected_sources"] = protected_sources
                if any(files.get(name) != source for name, source in protected_sources.items()):
                    raise ValueError("protected evaluator changed; a new protocol is required")
                allowed = contract["protocol"].get("allowed_files") or []
                if allowed and set(files) - set(allowed) - set(protected):
                    raise ValueError("candidate changes files outside the research contract")
                candidate = evidence_service.snapshot_candidate(
                    files, parent_id=state.get("working_candidate_id"),
                    change_type=str(state.get("next_change_type") or "method"),
                    environment={"container": (experiment.plan or {}).get("container"),
                                 "requirements": files.get("requirements.txt")},
                )
                candidates = list(state.get("candidates") or [])
                if not any(c["candidate_id"] == candidate["candidate_id"] for c in candidates):
                    candidates.append(candidate)
                state["candidates"] = candidates
                state["working_candidate_id"] = candidate["candidate_id"]
                run_id = uuid.uuid4()
                run = ExperimentRun(id=run_id, experiment_id=experiment.id, seq=seq,
                                    command="bash run.sh", status="running", started_at=utcnow())
                meta = {"step_id": step_id, "workspace_id": str(run_id),
                        "candidate_id": candidate["candidate_id"],
                        "contract_id": contract["contract_id"],
                        "protocol_id": contract["protocol_id"]}
                metadata[str(run_id)] = meta
                state["run_metadata"] = metadata
                state["remote_cleanup"] = {"status": "pending", "reason": "launching"}
                experiment.iteration_state = state
                session.add(run)
                if not await _commit_action(session, ctx, publication=True):
                    return {"cancelled": True}
                # JSON state becomes the ORM's committed value; never mutate its
                # nested aliases while preparing a durable launch reservation.
                state = _iteration_state(experiment)
                metadata = state["run_metadata"]
                meta = metadata[str(run_id)]
                await executor.prepare_run_workspace(str(run_id), files)
                meta["workspace_prepared"] = True
                state["run_metadata"] = metadata
                experiment.iteration_state = state
                await _commit_action(session, ctx)
                managed_handle = await launch_with_budget()
                if managed_handle is not None:
                    run.pid = managed_handle.process_id
                    run.command = managed_handle.context.display_command
                run.log_path = str(experiments_service.append_local_log(experiment.id, seq, ""))
            if managed_handle is None and run.pid is None:
                # Preparation used the last remaining budget; no remote job was launched.
                run.status, run.exit_code, run.finished_at = "failed", -15, utcnow()
                state["stopped_reason"] = "max_hours"
                state.pop("remote_cleanup", None)
                state["result_bundle"] = _result_bundle(experiment, state)
                experiment.iteration_state = state
                await _commit_action(session, ctx)
                iterate_cp["stopped_reason"] = "max_hours"
                return {"skipped": True, "seq": run.seq, "stopped_reason": "max_hours",
                        "plan_signal": {"decision": "finish", "stopped_reason": "max_hours"}}
            if managed_handle is not None:
                # The absolute controller deadline below survives reconnects and restarts.
                state = _iteration_state(experiment)
                state["remote_cleanup"] = {"status": "pending", "reason": "running",
                                           "workspace_id": meta.get("workspace_id"),
                                           "handle": _serialize_managed_handle(managed_handle)}
                experiment.iteration_state = state
                await _commit_action(session, ctx)
                try:
                    snapshot, executor = await _monitor_managed_command(
                        ctx, session, executor, experiment, managed_handle, run=run
                    )
                except ManagedCommandNeedsUser as pending:
                    return _managed_command_waiting_result(ctx, experiment, pending)
                except ManagedCommandCancelled as cancelled:
                    executor = cancelled.executor
                    return {"cancelled": True, "run_id": str(run.id), "seq": run.seq}
                run.exit_code = snapshot.exit_status
                run.status = "succeeded" if snapshot.exit_status == 0 else "failed"
                run.finished_at = utcnow()
                observation = {"run_id": str(run.id), "seq": run.seq,
                               "exit_code": run.exit_code, "run_status": run.status}
                if snapshot.exit_status != 0:
                    observation["failure"] = failure_from_snapshot(snapshot).to_dict()
            else:
                observation, executor = await _poll_run(
                    ctx, session, executor, experiment, run, max_hours
                )
            if observation.get("cancelled"):
                return observation
            # Legacy root metrics have no run identity; never reuse them for a recovered run.
            if meta.get("workspace_id"):
                metrics_text = await executor.read_metrics_json()
                if metrics_text:
                    points = _evaluation_metrics_json(metrics_text)
                    run.metrics = merge_metrics(run.metrics, points)
            run.primary_value = extract_primary_value(run.metrics, pm.get("selector", pm["name"]))
            state = _iteration_state(experiment)
            if candidate is not None:
                evaluation = evidence_service.make_evaluation(
                    contract=contract, candidate=candidate, run_id=str(run.id), seq=run.seq,
                    status=run.status, exit_code=run.exit_code, metrics=run.metrics or {},
                    primary_value=run.primary_value, identity=meta,
                    command=run.command, log_path=run.log_path,
                    job_identity={"operation_id": managed_handle.operation_id,
                                  "attempt_id": managed_handle.attempt_id}
                    if managed_handle else {"pid": run.pid},
                )
                # Snapshot bytes are authoritative. Candidate-modified evaluator/source
                # cannot pass even when it reports an attractive metric.
                changed_sources = []
                for name, source in candidate["files"].items():
                    try:
                        actual = (await executor.read_file(name)).decode("utf-8")
                    except Exception as exc:
                        if ssh_exec.is_connection_error(exc):
                            raise
                        actual = None
                    if actual != source:
                        changed_sources.append(name)
                if changed_sources:
                    evaluation["valid"] = False
                    evaluation["invalid_reasons"].append(
                        "runtime_source_changed:" + ",".join(sorted(changed_sources))
                    )
                old_incumbent = (state.get("incumbent") or {}).get("evaluation_id")
                already_recorded = any(e["evaluation_id"] == evaluation["evaluation_id"]
                                       for e in state.get("evaluations", []))
                state = evidence_service.promote_candidate(state, evaluation, candidate, contract)
                if evaluation["valid"] and not already_recorded:
                    new_incumbent = (state.get("incumbent") or {}).get("evaluation_id")
                    state["no_improve_streak"] = (0 if new_incumbent != old_incumbent else
                                                  state["no_improve_streak"] + 1)
                elif not evaluation["valid"] and not already_recorded:
                    state["invalid_evaluation_count"] = int(state.get("invalid_evaluation_count")
                                                             or 0) + 1
                observation["evaluation_valid"] = evaluation["valid"]
                observation["invalid_reasons"] = evaluation["invalid_reasons"]
            else:
                observation["evaluation_valid"] = False
            # Failed/partial metrics stay on their run for diagnostics, outside scientific facts.
            valid_evaluations = [e for e in state.get("evaluations", []) if e.get("valid")]
            aggregate = {}
            for evaluation in valid_evaluations:
                for name, series in evaluation["metrics"].items():
                    aggregate.setdefault(name, []).extend(series)
            experiment.metrics = aggregate
            iterate_cp["primary_metric_runs"] = len(valid_evaluations)
            state["result_bundle"] = _result_bundle(experiment, state)
            experiment.iteration_state = state
            diagnostic = {"id": run.id, "seq": run.seq,
                          "metrics": json.loads(json.dumps(run.metrics or {})),
                          "primary_value": run.primary_value, "exit_code": run.exit_code,
                          "status": run.status, "finished_at": run.finished_at}
            if not await _commit_action(session, ctx, publication=True):
                await session.refresh(experiment)
                run = await session.get(ExperimentRun, diagnostic["id"])
                run.metrics = diagnostic["metrics"]
                run.primary_value = diagnostic["primary_value"]
                if run.status == "running":
                    run.status = diagnostic["status"]
                    run.exit_code = diagnostic["exit_code"]
                    run.finished_at = diagnostic["finished_at"]
                persisted_state = _iteration_state(experiment)
                iterate_cp["primary_metric_runs"] = sum(
                    bool(e.get("valid")) for e in persisted_state.get("evaluations", [])
                )
                await _commit_action(session, ctx)
                if experiment.status not in EXPERIMENT_TERMINAL_STATUSES:
                    await _set_status(ctx, session, experiment, "cancelled")
                return {"cancelled": True, "run_id": str(diagnostic["id"]),
                        "seq": diagnostic["seq"]}
        finally:
            await executor.close()
        if await _voyage_cancelled(session, ctx):
            await session.refresh(experiment)
            if experiment.status not in EXPERIMENT_TERMINAL_STATUSES:
                await _set_status(ctx, session, experiment, "cancelled")
            return {"cancelled": True, "run_id": str(run.id), "seq": run.seq}
        return {**observation, "primary_value": run.primary_value,
                "metric_names": sorted((run.metrics or {}).keys())}


def _result_bundle(experiment: Experiment, state: dict[str, Any]) -> dict[str, Any]:
    return evidence_service.build_result_bundle(
        experiment_id=str(experiment.id), contract=state["research_contract"],
        candidates=state.get("candidates", []), evaluations=state.get("evaluations", []),
        selected_candidate_id=(state.get("incumbent") or {}).get("candidate_id"),
        verdicts=state.get("scientific_verdicts", []), stopped_reason=state.get("stopped_reason"),
    )


async def _restore_selected_source(
    ctx: ActionContext, experiment: Experiment, executor: Runner
) -> dict[str, Any]:
    state = _iteration_state(experiment)
    bundle = _result_bundle(experiment, state)
    selected = bundle.get("selected_candidate")
    if selected:
        tracked = set(ctx.checkpoint.get("exp_files") or {})
        for candidate in state.get("candidates", []):
            tracked.update(candidate.get("files") or {})
        obsolete = sorted(tracked - set(selected["files"]))
        await executor.remove_candidate_files(obsolete)
        ctx.checkpoint["exp_files"] = dict(selected["files"])
        await executor.write_files(selected["files"])
    await executor.write_files({"result_bundle.json": json.dumps(bundle, ensure_ascii=False)})
    state["result_bundle"] = bundle
    experiment.iteration_state = state
    return bundle


@register("experiment.analyze")
@_guarded
async def experiment_analyze(ctx: ActionContext, params: dict[str, Any]) -> dict[str, Any]:
    """单轮分析：structured reflection → 假设回写 → 终止判定 → improve/debug 改代码。

    产出 plan_signal 供引擎的确定性分支表消费（docs/task-system.md §7（原 voyage-loop.md §7））：
    - continue：已按 reflection 改完代码，追加下一轮 run + analyze；
    - finish：终止条件命中（stop/假设定论/无提升/预算/debug 限额），进入收尾。
    终止判定顺序与原 experiment.iterate 完全一致。
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        experiment = await _get_experiment(session, ctx)

        if (_iteration_state(experiment).get("remote_cleanup") or {}):
            # Finish the previous dependency repair before proposing or writing new files.
            executor = await _open_executor(session, ctx, experiment)
            try:
                _snapshot, executor, waiting = await _run_managed_phase(
                    ctx, session, executor, experiment, phase="dependency.install",
                    operation="dependency-install", command="reconcile prior dependency repair",
                    launch_method="launch_managed_setup", reconcile_only=True,
                )
                if waiting is not None:
                    return waiting
            finally:
                await executor.close()

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
            # 计划调整可能把本轮 run 作废/漏排（如换方案后直接落到 analyze）。
            # 报错会触发又一轮 LLM 重排（实测螺旋出跨域乱步骤）；确定性自愈：
            # 发 plan_signal 让分支表补一轮 run + analyze，从头跑起。
            await ctx.log("没有可分析的运行轮次，自动补一轮运行")
            return {
                "skipped": True,
                "reason": "no_runs",
                "plan_signal": {"decision": "continue", "next_round": 1},
            }
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
        contract = state["research_contract"]
        current_evaluation = next((e for e in state.get("evaluations", [])
                                   if e["run_id"] == str(run.id)), None)
        cond_delta = evidence_service.condition_summary(run.metrics or {}, contract)
        cond_line = (
            f"对照汇总（baseline vs treatment，平台确定性计算）："
            f"{json.dumps(cond_delta, ensure_ascii=False)}\n"
            if cond_delta
            else ""
        )
        run_lasts = {k: _last_value(v) for k, v in (run.metrics or {}).items()}
        # 用户在对话里的建议 / 对提问的回答（引擎注入 step params，见 docs/task-system.md）
        _remember_guidance(ctx, params)
        guidance_line = _guidance_line(params)
        reflection_user = (
            f"实验计划：{json.dumps(plan, ensure_ascii=False)[:4000]}\n"
            f"冻结研究契约（权威目标与判据）：{json.dumps(contract, ensure_ascii=False)}\n"
            f"本轮有效性与证据引用：{json.dumps(current_evaluation, ensure_ascii=False)}\n"
            f"主指标：{json.dumps(pm, ensure_ascii=False)}（假设共 {hyp_count} 条）\n"
            f"{_memory_prompt(ctx)}"
            f"{guidance_line}"
            f"本轮运行：seq={run.seq} status={run.status} exit_code={run.exit_code} "
            f"primary_value={run.primary_value}\n"
            f"本轮各指标末值：{json.dumps(run_lasts, ensure_ascii=False)[:1500]}\n"
            f"{cond_line}"
            f"历史各轮：{json.dumps(history, ensure_ascii=False)}\n"
            f"迭代状态：无提升连续 {state['no_improve_streak']} 轮，"
            f"debug 已修 {state['debug_count']} 次\n"
            f"本轮日志尾部：\n" + "\n".join(log_lines)
        )
        reflection = await _complete_json(
            ctx,
            system=REFLECTION_SYSTEM_PROMPT,
            user=reflection_user,
            validate=validate_reflection,
        )
        run.reflection = reflection
        _remember(
            ctx,
            f"第 {run.seq} 轮结论",
            f"主指标 {run.primary_value}；决策 {reflection['decision']}；"
            f"诊断：{str(reflection.get('diagnosis') or '')[:250]}"
            + (
                f"；下一步：{str(reflection.get('planned_change') or '')[:250]}"
                if reflection.get("planned_change")
                else ""
            ),
        )
        if reflection.get("memory_note"):
            _remember(ctx, "AI 笔记", str(reflection["memory_note"])[:600])

        # 尝试存档（通用先验经验档案）：把本轮实现的源码/得分/轨迹存起来，供后续迭代 proposer 读取
        # 全量历史（不是只看上一轮）。记录产生本轮 run 的实现（当前 exp_files）。
        archive = list(ctx.checkpoint.get("attempt_archive") or [])
        meta = (state.get("run_metadata") or {}).get(str(run.id), {})
        candidate = next((c for c in state.get("candidates", [])
                          if c["candidate_id"] == meta.get("candidate_id")), None)
        archive = [entry for entry in archive if entry.get("seq") != run.seq]
        archive.append(
            {
                "seq": run.seq,
                "primary_value": run.primary_value,
                "conditions_delta": cond_delta,
                "files": dict(candidate["files"]) if candidate else {},
                "valid": bool(current_evaluation and current_evaluation.get("valid")),
                "evaluation_id": (current_evaluation or {}).get("evaluation_id"),
                "candidate_id": meta.get("candidate_id"),
                "trace": "\n".join(log_lines[-30:]),
                "observation": reflection.get("observation"),
            }
        )
        ctx.checkpoint["attempt_archive"] = archive

        # 假设回写 + iteration_state 落库
        plan, verdicts = evidence_service.apply_scientific_verdicts(
            plan, reflection["hypothesis_updates"], contract=contract,
            evaluations=state.get("evaluations", []),
        )
        old_verdicts = {v["claim_id"]: v for v in state.get("scientific_verdicts", [])}
        old_verdicts.update({v["claim_id"]: v for v in verdicts})
        state["scientific_verdicts"] = list(old_verdicts.values())
        authority = _result_bundle(experiment, state)
        authoritative = {v["claim_id"]: v for v in authority["scientific_verdicts"]}
        for claim in contract["claims"]:
            if claim["index"] >= len(plan["hypotheses"]):
                continue
            hypothesis = plan["hypotheses"][claim["index"]]
            verdict = authoritative.get(claim["claim_id"]) or {}
            if hypothesis.get("text") != claim["text"] or (
                "criterion" in hypothesis and hypothesis["criterion"] != claim.get("criterion")
            ):
                verdict = {}
            hypothesis["scientific_verdict"] = verdict.get("verdict", "inconclusive")
            hypothesis["status"] = {"supported": "verified", "refuted": "falsified"}.get(
                hypothesis["scientific_verdict"], "testing"
            )
        experiment.plan = plan
        experiment.iteration_state = dict(state)
        if not await _commit_action(session, ctx, publication=True):
            return {"cancelled": True}

        # decision=ask：AI 拿不准，向用户提问（引擎收到 observation.ask 转 paused_ask；
        # 回答后本步骤重跑，回答文本经 params["user_guidance"] 注入上面的反思 prompt）
        decision = reflection["decision"]
        if decision == "ask":
            question = reflection.get("question") or "AI 需要你的判断才能继续，这轮结果怎么处理？"
            return {
                "seq": run.seq,
                "decision": decision,
                "rounds": len(runs),
                "ask": {
                    "ask_kind": "action_ask",
                    "question": question,
                    "context": {
                        "observation": reflection.get("observation"),
                        "diagnosis": reflection.get("diagnosis"),
                        "primary_value": run.primary_value,
                        "history": history[-6:],
                    },
                },
            }

        # decision 分支与终止条件（顺序与原 iterate 一致，docs/task-system.md §7）
        resolved_claims = {v["claim_id"] for v in authority["scientific_verdicts"]
                           if v["verdict"] in {"supported", "refuted"}}
        iterate_cp = dict(ctx.checkpoint.get("iterate") or {})
        iterate_started = (
            datetime.fromisoformat(str(iterate_cp["started_at"]))
            if iterate_cp.get("started_at")
            else utcnow()
        )
        stopped_reason: str | None = None
        if decision == "stop":
            state["proposed_stop_reason"] = reflection.get("stop_reason")
            stopped_reason = "decision_stop"
        elif contract["claims"] and all(
            claim["claim_id"] in resolved_claims for claim in contract["claims"]
        ):
            stopped_reason = "hypotheses_resolved"
        elif state["no_improve_streak"] >= no_improve_stop:
            stopped_reason = "no_improve"
        elif max_runs and run.seq >= max_runs:
            stopped_reason = "max_runs"
        elif max_hours and _elapsed_hours(iterate_started) > max_hours:
            stopped_reason = "max_hours"
        # debug 不再按次数终止（用户定调：只受时间/轮数预算约束）——
        # debug_count 仍然记账，供面板与 reflection 观察

        if stopped_reason:
            _remember(ctx, "终止判定", f"迭代结束：{stopped_reason}")
            state["stopped_reason"] = stopped_reason
            state["result_bundle"] = _result_bundle(experiment, state)
            experiment.iteration_state = dict(state)
            await _commit_action(session, ctx)
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
            await _commit_action(session, ctx)
        state["next_change_type"] = "repair" if decision == "debug" else "method"
        experiment.iteration_state = state
        await _commit_action(session, ctx)

        # improve → 迭代优化 proposer（读全量尝试档案提下一候选）；debug → 按报错修当前文件
        files: dict[str, str] = dict(ctx.checkpoint.get("exp_files") or {})
        if decision == "debug":
            system_prompt = _prompt_with_context(DEBUG_SYSTEM_PROMPT, ctx)
            # 失败诊断也带上「历史尝试档案」：让 debug 能看见前面试过什么、哪些方案已被证伪，
            # 从而做方案级调整（换依赖/框架/加载方式）而非反复在同一条死路上改代码。
            prior = archive[:-1]  # 除当前失败轮外的历史尝试
            archive_ctx = (
                _render_attempt_archive(prior, direction=pm.get("direction", "maximize"))
                if prior else ""
            )
            fix_user = (
                (archive_ctx + "\n" if archive_ctx else "")
                + _memory_prompt(ctx)
                + guidance_line
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
                _render_attempt_archive(archive, direction=pm.get("direction", "maximize"))
                + "\n"
                + _memory_prompt(ctx)
                + guidance_line
                + f"\n主指标：{json.dumps(pm, ensure_ascii=False)}\n"
                + f"当前尝试 seq={run.seq} 主指标={run.primary_value}；"
                + f"reflection 诊断：{reflection['diagnosis']}\n"
                + f"reflection 改进方向（参考）：{reflection.get('planned_change') or '（无）'}\n"
                + "请综合以上全部尝试的源码/得分/轨迹，提出一个有依据的新尝试，"
                + "输出修改后的完整文件集合。"
            )
            # 反卡死：连续多轮主指标无提升 → 逼 proposer**换根本不同的方法**而非同方向微调
            # （呼应「让 Agent 不断找到方法，不只写代码」——诊断到瓶颈就换方案，不是原地打磨）。
            if state["no_improve_streak"] >= 1:
                fix_user += (
                    f"\n\n⚠️ 已连续 {state['no_improve_streak']} 轮主指标无提升——"
                    "**不要再在同一方向上微调**。请换一个**根本不同的方法/思路**"
                    "（例如：不同的算法/建模方式/训练目标或损失/数据处理/检索或提示策略等），"
                    "先用一句话说明为什么之前那条路已经到顶、你这次新方向的依据，再给完整文件集合。"
                )
        prev_files = ctx.checkpoint.get("exp_files") or {}
        files = await _complete_json(
            ctx, system=system_prompt, user=fix_user, validate=validate_files
        )
        allowed = contract["protocol"].get("allowed_files")
        protected = state.get("protected_sources") or {}
        if allowed and set(files) - set(allowed) - set(protected):
            raise ValueError("candidate changes files outside the frozen contract")
        if any(files.get(name) != source for name, source in protected.items()):
            raise ValueError("protected evaluator changed; start a new protocol and baseline")
        executor = await _open_executor(session, ctx, experiment)
        try:
            await executor.write_files(files)
            if files.get("requirements.txt") != prev_files.get("requirements.txt"):
                # 与冒烟修复循环同理：requirements 变更必须真正重装（增量 pip），
                # 否则下一轮 run 还在旧依赖上跑
                await ctx.log("requirements.txt 有变更，重装依赖供下一轮使用")
                dep, executor, waiting = await _run_managed_phase(
                    ctx, session, executor, experiment, phase="dependency.install",
                    operation="dependency-install", command="install experiment dependencies",
                    launch_method="launch_managed_setup",
                )
                if waiting is not None:
                    return waiting
                if dep.exit_status != 0:
                    await ctx.log(
                        f"依赖重装未成功（exit={dep.exit_status}），下一轮将带错重修",
                        level="warn",
                    )
            await _sync_memory_file(ctx, executor)
        finally:
            await executor.close()
        ctx.checkpoint["exp_files"] = files
        iterate_cp["last_completed_seq"] = run.seq
        ctx.checkpoint["iterate"] = iterate_cp
        await _commit_action(session, ctx)

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
    executor: Runner,
    experiment: Experiment,
    run: ExperimentRun,
    max_hours: float,
) -> tuple[dict[str, Any], Runner]:
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
        await _commit_action(session, ctx)

    async def finish(exit_code: int | None) -> dict[str, Any]:
        try:
            await ingest_chunk()  # 收尾：抓最后一段日志
        except Exception as e:  # noqa: BLE001 — 收尾抓日志断连不该翻盘
            if not ssh_exec.is_connection_error(e):
                raise
        run.exit_code = exit_code
        run.status = "succeeded" if exit_code == 0 else "failed"
        run.finished_at = utcnow()
        await _commit_action(session, ctx)
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
        workspace_id = getattr(executor, "run_workspace_id", None)
        executor = await _open_executor(session, ctx, experiment)
        if workspace_id:
            executor.bind_run_workspace(workspace_id)

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
            await _commit_action(session, ctx)
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
            await _commit_action(session, ctx)
            await asyncio.sleep(_reconnect_backoff(conn_fail_streak))
            try:
                await reconnect()
            except Exception as re:  # noqa: BLE001 — 重连本身失败：下轮继续退避重试
                if not ssh_exec.is_connection_error(re):
                    raise
            continue  # 远端状态持久化，重连后下一轮继续跟踪

        if max_hours and _elapsed_hours(run.started_at) > max_hours:
            try:
                await executor.kill_pid(int(run.pid or 0))
                await ingest_chunk()
            except Exception as e:  # noqa: BLE001
                if not ssh_exec.is_connection_error(e):
                    raise
            run.status = "failed"
            run.finished_at = utcnow()
            await _commit_action(session, ctx)
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


async def _pull_figures(executor: Runner, experiment_id: uuid.UUID, names: list[str]) -> list[str]:
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
        plan = experiment.plan or {}
        executor = await _open_executor(session, ctx, experiment)
        try:
            bundle = await _restore_selected_source(ctx, experiment, executor)
            await _commit_action(session, ctx)
        finally:
            await executor.close()
        if ctx.checkpoint.get("plot_bundle_id") != bundle["bundle_id"]:
            ctx.checkpoint.pop("plot_files", None)
            ctx.checkpoint["plot_bundle_id"] = bundle["bundle_id"]
        # Failed/partial evaluations are diagnostic evidence, outside plotting data.
        metrics_all = {
            "bundle_id": bundle["bundle_id"],
            "selected_candidate_id": (bundle.get("selected_candidate") or {}).get("candidate_id"),
            "primary_metric": plan.get("primary_metric"),
            "runs": bundle["valid_evaluations"],
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
                        f"{_guidance_line(params)}"
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
                plot_id = str(uuid.uuid4())
                await executor.prepare_run_workspace(
                    plot_id, {**plot_files, "metrics_all.json": metrics_all_text}
                )
                attempts = list(ctx.checkpoint.get("plot_attempts") or [])
                attempts.append({"workspace_id": plot_id, "bundle_id": bundle["bundle_id"],
                                 "fix": fixes})
                ctx.checkpoint["plot_attempts"] = attempts
                await _sync_memory_file(ctx, executor)

                # Plot preparation and execution retain durable identities through cancellation.
                _deps, executor, waiting = await _run_managed_phase(
                    ctx, session, executor, experiment, phase="dependency.plot",
                    operation="plot-dependencies", command="install plotting dependencies",
                    launch_method="launch_managed_plot_deps",
                )
                if waiting is not None:
                    return waiting
                result, executor, waiting = await _run_managed_phase(
                    ctx, session, executor, experiment, phase="application.plot",
                    operation="experiment-plot", command="python plot_figures.py",
                    launch_method="launch_managed_plot",
                )
                if waiting is not None:
                    return waiting
                if result.exit_status != 0:
                    entries = []
                    problem = (
                        f"脚本执行失败（exit={result.exit_status}）："
                        f"{(result.stderr_tail or result.stdout_tail)[-_STDERR_CHARS:]}"
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

        experiment.figures = [{**entry, "bundle_id": bundle["bundle_id"]} for entry in entries]
        state = _iteration_state(experiment)
        state["figures_bundle_id"] = bundle["bundle_id"]
        experiment.iteration_state = state
        if not await _commit_action(session, ctx, publication=True):
            return {"cancelled": True}

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
        state = _iteration_state(experiment)
        executor = await _open_executor(session, ctx, experiment)
        try:
            bundle = await _restore_selected_source(ctx, experiment, executor)
            await _commit_action(session, ctx)
        finally:
            await executor.close()
        selected_ids = {e["run_id"] for e in bundle["selected_evaluations"]}
        last_run = next((r for r in reversed(runs) if str(r.id) in selected_ids), None)
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
        cond_delta = evidence_service.condition_summary(
            last_run.metrics or {}, state["research_contract"]
        ) if last_run else None
        cond_line = (
            f"对照汇总（baseline vs treatment，平台确定性计算）："
            f"{json.dumps(cond_delta, ensure_ascii=False)}\n"
            if cond_delta
            else ""
        )
        bundle_prompt = {**bundle, "selected_candidate": {
            key: value for key, value in (bundle.get("selected_candidate") or {}).items()
            if key != "files"
        }}
        bundle_prompt["candidate_history"] = [
            {key: value for key, value in candidate.items() if key != "files"}
            for candidate in bundle.get("candidate_history", [])
        ]
        user_prompt = (
            f"权威证据包（只能据此写事实，失败数据仅作诊断）："
            f"{json.dumps(bundle_prompt, ensure_ascii=False)}\n"
            f"实验计划：{json.dumps(experiment.plan or {}, ensure_ascii=False)[:4000]}\n"
            f"{_memory_prompt(ctx)}"
            f"{_guidance_line(params)}"
            f"迭代各轮：{json.dumps(runs_brief, ensure_ascii=False)}\n"
            f"停止原因：{state.get('stopped_reason')}\n"
            f"指标数据：{json.dumps(experiment.metrics or {}, ensure_ascii=False)[:4000]}\n"
            f"{cond_line}"
            f"日志尾部：\n" + "\n".join(log_lines)
        )
        result = await ctx.llm.complete(
            "experiment",
            [
                Message(
                    role="system",
                    content=REPORT_SYSTEM_PROMPT + ctx.evidence_guidance(),
                ),
                Message(role="user", content=user_prompt),
            ],
            user_id=ctx.run.created_by,
            project_id=ctx.run.project_id,
            voyage_id=ctx.run.id,
        )
        experiment.report = result.content.strip()
        state = _iteration_state(experiment)
        state["report_bundle_id"] = bundle["bundle_id"]
        experiment.iteration_state = state
        _remember(ctx, "报告", f"实验报告已生成（约 {len(experiment.report)} 字）")
        run_ok = last_run is not None and last_run.status == "succeeded"
        # 报告步**完全不写终态**（#367 去掉了提前 failed；线上随后实测提前 done 同样
        # 有害：done_criteria 后置失败转提问时 waiting_user 镜像被终态挡住，实验显示
        # done 而 voyage 在等人）。done 统一由 voyage 终态联动落定
        # （engine._set_status(done) → experiments_service.complete_by_voyage），
        # failed 只由人拍板。
        if not run_ok:
            await ctx.log("最后一轮运行未成功——不自动判失败，等完成标准检查和你的裁决")
        session.add(
            Activity(
                project_id=experiment.project_id,
                actor="agent:experiment",
                kind="experiment.completed",
                message="实验报告已生成",
                payload={"experiment_id": str(experiment.id), "last_run_ok": run_ok},
            )
        )
        if not await _commit_action(session, ctx, publication=True):
            return {"cancelled": True}

    # voyage 级完成标准（done_criteria）断言该标记：防"过早宣告完成"
    ctx.checkpoint["report_done"] = True
    return {
        "report_chars": len(experiment.report or ""),
        "last_run_ok": run_ok,
        "usage": result.usage,
    }
