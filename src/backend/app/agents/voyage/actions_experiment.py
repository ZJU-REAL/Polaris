"""experiment voyage 动作（kind ``experiment``，docs/api-m5-a.md §1 + docs/voyage-loop.md §7）。

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
import functools
import json
import math
import re
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.voyage.actions import ActionContext, register
from app.agents.voyage.runner import Runner, open_runner, parse_container_spec
from app.core.db import get_sessionmaker
from app.core.llm.base import Message
from app.models.activity import Activity
from app.models.base import utcnow
from app.models.experiment import EXPERIMENT_TERMINAL_STATUSES, Experiment, ExperimentRun
from app.models.idea import Idea
from app.models.library_direction import LibraryPaper
from app.models.paper import Paper, PaperWiki
from app.models.ssh_credential import SSHCredential
from app.models.voyage import VoyageRun, VoyageStep
from app.services import experiment_settings as experiment_settings_service
from app.services import experiments as experiments_service
from app.services import ssh_exec
from app.services import voyage_messages as messages_service
from app.services.figure_annotate import prepare_image_for_llm
from app.services.libraries import (
    dedupe_member_rows,
    get_source_library_ids,
    member_papers_stmt,
)

RUN_POLL_SECONDS = 30.0  # 正式运行轮询间隔（测试 monkeypatch 为 0）
MAX_SETUP_FIXES = 2  # 依赖安装失败回 LLM 修 requirements/run.sh 的次数上限
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
{"kind": "eval|training|agent|analysis|other",
 "hypotheses": [{"text": "可检验的假设", "status": "testing"}],
 "repro_strategy": "基线复现策略（官方代码 > 可信第三方 > 自重写 > 仅引用数字）",
 "steps": ["实验步骤 1", "实验步骤 2"],
 "primary_metric": {"name": "主指标名", "direction": "maximize"},
 "conditions": [{"name": "baseline", "role": "baseline", "description": "对照组"},
                {"name": "treatment_a", "role": "treatment", "description": "处理组"}],
 "eval_protocol": {"dataset": "数据集/来源", "split": "评测划分", "metric": "评测指标",
                   "n_examples": 100, "n_samples": 1},
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
        return (1, float(v)) if isinstance(v, int | float) else (0, float("-inf"))

    best = max(archive, key=_score)
    recent = set(map(id, archive[-max(recent_full, 0) :]))
    parts = [f"历史尝试档案（共 {len(archive)} 次；最优与最近 {recent_full} 次含源码全文）："]
    for c in archive:
        star = " ★迄今最好" if c is best else ""
        delta = c.get("conditions_delta")
        delta_s = json.dumps(delta, ensure_ascii=False) if delta else "—"
        header = (
            f"\n[尝试 seq={c.get('seq')} | 主指标={c.get('primary_value')}{star}"
            f" | 对照={delta_s}]"
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


def _env_facts_prompt(env_settings: dict[str, str]) -> str:
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
        lines.append(f"- 本机数据集都放在 {root} 下（环境变量 $POLARIS_DATASET_ROOT）。")
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
    if not lines:
        return ""
    return "\n\n本机环境（平台实配，按此写代码，不要臆测）：\n" + "\n".join(lines)


def diagnose_failure(err_text: str, env_settings: dict[str, str] | None = None) -> str:
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
    env_settings: dict[str, str] | None = None,
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


async def _mark_attention(ctx: ActionContext, reason: str) -> None:
    """异常路径：只留 Activity 痕迹，不再抢先把实验打成 failed。

    命运交给引擎的失败分派——原地重试 / 计划调整 / 转向用户提问（paused_ask）
    都可能救回来；提前写死终态后 EXPERIMENT_TERMINAL_STATUSES 检查会挡住一切
    后续状态更新，任务明明救活了实验行却永远躺在 failed。failed 只在两处写：
    闸门驳回联动与用户回答「放弃」（都走 experiments_service.fail_by_voyage）。
    """
    async with get_sessionmaker()() as session:
        experiment = await session.get(Experiment, _experiment_id(ctx))
        if experiment is None or experiment.status in EXPERIMENT_TERMINAL_STATUSES:
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
        await session.commit()


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
                step = (
                    await session.get(VoyageStep, ctx.step_id) if ctx.step_id else None
                )
                if step is not None:
                    step_params = dict(step.params or {})
                    step_params["user_guidance"] = merged
                    step.params = step_params
                messages_service.mark_chat_consumed(pending, step_id=ctx.step_id)
                await session.commit()
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
        except Exception as e:
            await _mark_attention(ctx, f"{type(e).__name__}: {e}")
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
    """
    last_error: Exception | None = None
    for attempt in range(_MAX_JSON_ATTEMPTS):
        prompt = user
        if last_error is not None:
            prompt = (
                f"{user}\n\n---\n"
                f"上一次输出没通过校验（第 {attempt} 次尝试）：{last_error}\n"
                "请针对这个错误修正后重新输出完整 JSON，不要重复同样的问题。"
            )
        result = await ctx.llm.complete(
            "experiment",
            [Message(role="system", content=system), Message(role="user", content=prompt)],
            user_id=ctx.run.created_by,
            project_id=ctx.run.project_id,
            voyage_id=ctx.run.id,
        )
        try:
            return validate(_extract_json(result.content))
        except (ValueError, TypeError, KeyError, json.JSONDecodeError) as e:
            last_error = e
    raise ValueError(f"LLM 连续输出非法 JSON：{last_error}")


async def _open_executor(
    session: AsyncSession, ctx: ActionContext, experiment: Experiment
) -> Runner:
    """为实验打开执行后端（Runner）。执行细节走 Runner 抽象，实验逻辑不直接依赖 SSH。

    kind 从 plan 里取（预留分派点：以后训练类→ContainerRunner 等），目前所有 kind 都用
    RemoteHostRunner，行为与之前一致。"""
    if experiment.credential_id is None:
        raise ValueError("实验缺少 SSH 凭据（credential_id 为空）")
    credential = await session.get(SSHCredential, experiment.credential_id)
    if credential is None:
        raise ValueError("SSH 凭据已删除，无法连接实验服务器")
    plan = experiment.plan if isinstance(experiment.plan, dict) else {}
    return await open_runner(
        credential=credential,
        exp_id=str(experiment.id),
        project_id=experiment.project_id,
        kind=plan.get("kind"),
        container=plan.get("container"),
    )


# ---- 计划 schema 校验 ----

_HYP_STATUSES = ("testing", "verified", "falsified")
_PM_DIRECTIONS = ("maximize", "minimize")
_DECISIONS = ("improve", "debug", "stop", "ask")
# 实验类型：驱动执行后端(Runner)与策略选择——eval 评测 / training 训练 / agent 智能体任务 /
# analysis 数据分析 / other 其它。plan 归类，向后兼容缺省 other。
_EXPERIMENT_KINDS = ("eval", "training", "agent", "analysis", "other")


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
    for name, content in files.items():
        rel = ssh_exec._validate_relpath(str(name))
        normalized[rel] = str(content)
    for required in ("requirements.txt", "run.sh"):
        if required not in normalized:
            raise ValueError(f"missing required file: {required}")
    if "--smoke" not in normalized["run.sh"]:
        raise ValueError("run.sh must support --smoke argument")
    _check_python_syntax(normalized)
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
    return math.isfinite(value)


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
            await session.commit()
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


async def _probe_resources(executor: Runner, plan: dict[str, Any]) -> tuple[list[dict], list[str]]:
    """资源预检（通用，不针对具体失败模式）：探 plan 声明的模型/数据集，把**事实**记进 resources
    （本机模型的 model_type/架构/配置分节、存在性），只对**普适**问题告警（声明的本机资源不存在）。
    ref 以 ~ 或 / 开头 = 本机路径；否则视为 HF id（会下载，跳过）。探测异常不冒泡，不崩 setup。"""
    resources: list[dict] = []
    warnings: list[str] = []
    for m in plan.get("models") or []:
        ref = (m.get("ref") if isinstance(m, dict) else str(m)) or ""
        if not ref:
            continue
        role = m.get("role") if isinstance(m, dict) else ""
        entry: dict[str, Any] = {"kind": "model", "ref": ref, "role": role}
        if ref.startswith(("~", "/")):  # 本机模型
            try:
                cfg = await executor.read_host_file(f"{ref}/config.json")
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
                exists = await executor.host_path_exists(str(name))
            except Exception:  # noqa: BLE001
                continue
            resources.append({"kind": "dataset", "ref": name, "found": exists})
            if not exists:
                warnings.append(f"资源预检告警：声明的本机数据集 {name} 不存在。")
    return resources, warnings


@register("experiment.setup")
@_guarded
async def experiment_setup(ctx: ActionContext, params: dict[str, Any]) -> dict[str, Any]:
    async with get_sessionmaker()() as session:
        experiment = await _get_experiment(session, ctx)
        await _set_status(ctx, session, experiment, "setup")

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
            await session.commit()
            await executor.write_files(platform_files)  # 平台文件写一次（不随修复变）

            # 资源预检（GPU + 模型/数据集）：确定性探测，记进观测（面板可见），有问题给早期告警。
            # **只告警不硬停**——资源问题硬停会触发 setup 换方案重规划（抹掉失败步骤、丢诊断）；
            # 真正的拦截（换机闸门）留后续，这刀先把资源可见性 + 早期告警做扎实。
            plan = experiment.plan if isinstance(experiment.plan, dict) else {}
            container = plan.get("container") if isinstance(plan.get("container"), dict) else None
            gpus = await executor.probe_gpu()
            resources, preflight_warnings = await _probe_resources(executor, plan)
            needs_gpu = plan.get("kind") == "training" or bool(container and container.get("gpus"))
            if needs_gpu and not gpus:
                preflight_warnings.append(
                    f"资源预检告警：{executor.host} 上探测不到可用 GPU（nvidia-smi 无输出），"
                    "但这是训练类/声明了 GPU 的实验——如后续因显存/设备失败，"
                    "请换一台有空闲 GPU 的服务器，或把方案改成不需要 GPU 的实现。"
                )

            # 依赖安装自愈（对称 smoke）：装不上/太慢/断连都当「可修的失败」而非硬崩——
            # 超时/断连→重连重试；pip 报错→回 LLM 修 requirements.txt/run.sh 再装（≤2 次）。
            fixes = 0
            attempts = 0
            while True:
                attempts += 1
                await executor.write_files(files)  # 每次（修复后）重写 LLM 产出文件
                hint = ""
                try:
                    # 后台脱离启动装依赖 + 轮询：轮询期间 SSH 瞬时断连可**重连接着跟同一安装进程**
                    # （而非从头重装），比同步单条命令更抗断连。退出码经 setup.exit 落盘再读。
                    pid, _cmd = await executor.launch_setup()
                    exit_status, executor = await _poll_setup(
                        ctx, session, executor, experiment, pid
                    )
                    err_text = ""
                    if exit_status != 0:
                        with contextlib.suppress(Exception):  # 读日志失败不该翻盘
                            err_text = (await executor.read_setup_log())[-_STDERR_CHARS:]
                except Exception as e:  # noqa: BLE001 — 超时/断连转成可修失败，其它异常照抛
                    if not (isinstance(e, TimeoutError) or ssh_exec.is_connection_error(e)):
                        raise
                    exit_status = -1
                    err_text = f"{type(e).__name__}: {e}"
                    hint = (
                        "依赖安装超时或中断——大概率是依赖太重/编译太慢/下载太慢或连接断开。"
                        "请精简依赖、用更轻的包或预编译 wheel、去掉可选依赖，让安装更快更稳。"
                    )
                    with contextlib.suppress(Exception):  # 超时后通道可能已坏，重连
                        await executor.close()
                    executor = await _open_executor(session, ctx, experiment)
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
                if fixes >= MAX_SETUP_FIXES:
                    # 修复额度用尽不再抛错：抛错会让引擎原地重试整个 setup——
                    # 又从头拉镜像/装依赖一遍（线上实测「一直装镜像」就是这个循环）。
                    # 转向用户提问，等人给指示再动。
                    detail = err_text or "（无输出，多为连接中断或超时）"
                    await _mark_attention(
                        ctx, f"依赖安装连续失败（{attempts} 次，exit={exit_status}）"
                    )
                    _remember(
                        ctx,
                        "环境障碍",
                        f"依赖安装 {attempts} 次未通过（exit={exit_status}）："
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
                                f"实验环境装不起来（尝试 {attempts} 次，"
                                f"自动修复 {fixes} 次已用尽），"
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
                # 把诊断 + 报错回给 LLM 修 requirements.txt/run.sh（方案级修复）。
                # 环境事实必须一起带上：修复循环原本只有 stderr，模型照样不知道这台机器
                # 上模型放哪、有没有配镜像源，只能接着猜。修复前先拉取对话流里
                # 新到的用户建议——装依赖动辄几十分钟，用户在旁边说话不能装听不见。
                fixes += 1
                hint = hint or diagnose_failure(err_text, env_settings)
                guidance_line = await _refresh_user_guidance(ctx, params)
                _remember_guidance(ctx, params)
                user_prompt = (
                    f"当前文件：{json.dumps(files, ensure_ascii=False)[:8000]}\n\n"
                    + _memory_prompt(ctx)
                    + guidance_line
                    + f"依赖安装退出码：{exit_status}\n"
                    + (f"诊断提示：{hint}\n" if hint else "")
                    + f"报错：\n{err_text}"
                    + _env_facts_prompt(env_settings)
                )
                files = await _complete_json(
                    ctx,
                    system=_prompt_with_context(SETUP_FIX_SYSTEM_PROMPT, ctx),
                    user=user_prompt,
                    validate=validate_files,
                )
                ctx.checkpoint["exp_files"] = files
        finally:
            await executor.close()


# ---- 3. 冒烟测试：exit 0 通过；失败回 LLM 修文件（≤2 次） ----


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
            while True:
                attempts += 1
                # 超时/断连也当作「可修的失败」（多为规模太大/太慢或环境问题），而非硬崩：
                # 诊断为「太慢/超时」→ 让 LLM 把冒烟改小改快再试，正是自适应循环该自愈的一类。
                hint = ""
                try:
                    result = await executor.run_smoke()
                    exit_status = result.exit_status
                    err_text = result.stderr or result.stdout
                except Exception as e:  # noqa: BLE001 — 超时/断连转成可修失败，其它异常照抛
                    if not (isinstance(e, TimeoutError) or ssh_exec.is_connection_error(e)):
                        raise
                    exit_status = -1
                    err_text = f"{type(e).__name__}: {e}"
                    hint = (
                        "冒烟超时或中断——大概率是模型/数据太大、步数太多、装依赖太慢。请把冒烟规模"
                        "改到极小：更小样本/更少步数/更小或更省显存的配置/精简依赖/缩短生成长度，"
                        "让 --smoke 很快跑完。"
                    )
                    with contextlib.suppress(Exception):  # 超时后通道可能已坏，重连
                        await executor.close()
                    executor = await _open_executor(session, ctx, experiment)
                if exit_status == 0:
                    if fixes:
                        _remember(ctx, "试跑", f"自动修复 {fixes} 次后通过")
                    await _sync_memory_file(ctx, executor)
                    return {"exit_code": 0, "attempts": attempts, "fixes": fixes}
                if fixes >= MAX_SMOKE_FIXES:
                    # 修复额度用尽不再抛错打死：转向用户提问（引擎收到 observation.ask
                    # 会把节点回 pending、任务转 paused_ask）。回答「重试」会从头重跑
                    # 本动作（修复计数随之清零），回答文本经 params["user_guidance"] 注入。
                    await _mark_attention(
                        ctx, f"冒烟测试连续失败（{attempts} 次，exit={exit_status}）"
                    )
                    _remember(
                        ctx,
                        "试跑障碍",
                        f"冒烟 {attempts} 次未通过（exit={exit_status}）："
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
                                f"代码试跑连续失败（{attempts} 次，自动修复 {fixes} 次已用尽），"
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
                fixes += 1
                hint = hint or diagnose_failure(err_text, env_settings)
                # 修复前拉取对话流里新到的用户建议（试跑修复同样是长循环）
                guidance_line = await _refresh_user_guidance(ctx, params)
                _remember_guidance(ctx, params)
                user_prompt = (
                    f"当前文件：{json.dumps(files, ensure_ascii=False)[:8000]}\n\n"
                    + _memory_prompt(ctx)
                    + guidance_line
                    + f"冒烟测试退出码：{exit_status}\n"
                    + (f"诊断提示：{hint}\n" if hint else "")
                    + f"stderr：\n{err_text[-_STDERR_CHARS:]}"
                    + _env_facts_prompt(env_settings)
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
        # 重启重挂：上一轮还挂着 running（worker 重启/任务被打断），远端 nohup 进程仍在跑
        # （或已留下 run.exit）——**重挂轮询同一进程**而不是再起一轮。否则新旧两棵进程树
        # 共写同一 workdir 的 run.log/run.exit（旧 launcher 晚到覆写 run.exit → 新轮读到假
        # 退出码；实测每次部署重启都会孤儿一轮训练）。_poll_run 天然处理三种情况：还活着
        # →继续跟；已结束→读 run.exit 收尾；死了没退出码→判 failed 交 analyze 诊断。
        stale = (
            prior_runs[-1]
            if prior_runs and prior_runs[-1].status == "running" and prior_runs[-1].pid
            else None
        )

        # 恢复现场护栏：预算已满就不再启动（正常路径由 analyze 的终止判定拦截；
        # 重挂不是新一轮，不受 max_runs 拦截）
        for reason, exhausted in (
            ("max_runs", bool(stale is None and max_runs and seq > max_runs)),
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
            if stale is not None:
                run = stale
                seq = run.seq
                # 重挂从日志头重放：清掉该轮已存的 metrics 避免重复合并（本地日志允许少量重复行）
                run.metrics = None
                await session.commit()
            else:
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
            # 记账：真正拿到主指标的轮次数。voyage 级完成标准据此判「这趟到底有没有
            # 结果」——否则每轮 exit 1、指标全空，只要写出了报告就照样宣告 done
            # （实测 voyage 6c5df454：三轮里两轮 exit 1，全被判 passed，最后 done，
            # 报告基于空数据。比直接失败更有害，因为它看起来是成功的）。
            iterate_cp = dict(ctx.checkpoint.get("iterate") or {})
            iterate_cp["primary_metric_runs"] = int(iterate_cp.get("primary_metric_runs") or 0) + 1
            ctx.checkpoint["iterate"] = iterate_cp
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
        cond_delta = _conditions_delta(experiment)
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
            f"主指标：{json.dumps(pm, ensure_ascii=False)}（假设共 {hyp_count} 条）\n"
            f"{_memory_prompt(ctx)}"
            f"{guidance_line}"
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

        # decision 分支与终止条件（顺序与原 iterate 一致，docs/api-m5-a.md §1）
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
            _remember(ctx, "终止判定", f"迭代结束：{stopped_reason}")
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
                _render_attempt_archive(archive)
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
        files = await _complete_json(
            ctx, system=system_prompt, user=fix_user, validate=validate_files
        )
        executor = await _open_executor(session, ctx, experiment)
        try:
            await executor.write_files(files)
            await _sync_memory_file(ctx, executor)
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


async def _poll_setup(
    ctx: ActionContext,
    session: AsyncSession,
    executor: Runner,
    experiment: Experiment,
    pid: int,
) -> tuple[int, Runner]:
    """轮询后台依赖安装直到 setup.exit 出现，返回 (exit_code, executor)。

    容错同 _poll_run：安装进程经 nohup 脱离、退出码持久化在 setup.exit——轮询期间 SSH 瞬时断连
    应「重连后接着跟同一安装进程」而非从头重装；连续多次重连失败才放弃。超 SETUP_TIMEOUT 未完
    则 kill 并抛 TimeoutError（上层当可修失败：让 LLM 精简依赖再来）。executor 可能因重连被替换。"""
    started = utcnow()
    conn_fail_streak = 0
    max_conn_fails = 6

    async def reconnect() -> None:
        nonlocal executor
        with contextlib.suppress(Exception):  # 旧连接已坏，关闭失败无所谓
            await executor.close()
        executor = await _open_executor(session, ctx, experiment)

    while True:
        try:
            exit_code = await executor.read_setup_exit()
            if exit_code is not None:
                return exit_code, executor
            if not await executor.check_pid(int(pid)):
                # 进程没了但没读到退出码：再读一次（竞态），仍无按 -1（异常收尾）
                again = await executor.read_setup_exit()
                return (again if again is not None else -1), executor
            conn_fail_streak = 0
        except Exception as e:  # noqa: BLE001 — 瞬时断连：重连续跟；其它异常照抛
            if not ssh_exec.is_connection_error(e):
                raise
            conn_fail_streak += 1
            if conn_fail_streak > max_conn_fails:
                raise RuntimeError(
                    f"SSH 反复断开（连续 {conn_fail_streak} 次），放弃跟踪依赖安装：{e}"
                ) from e
            await asyncio.sleep(_reconnect_backoff(conn_fail_streak))
            with contextlib.suppress(Exception):  # 重连失败：下轮继续退避重试
                await reconnect()
            continue  # setup.exit 持久化，重连后下一轮接着读
        if _elapsed_hours(started) * 3600.0 > ssh_exec.SETUP_TIMEOUT_SECONDS:
            with contextlib.suppress(Exception):
                await executor.kill_pid(int(pid))
            raise TimeoutError(f"依赖安装超过 {ssh_exec.SETUP_TIMEOUT_SECONDS:.0f}s 未完成，已中止")
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
                await executor.write_files(plot_files)
                await _sync_memory_file(ctx, executor)

                # 绘图依赖确定性保证（幂等；失败不阻断——真实错误由 run_plot 暴露）
                with contextlib.suppress(Exception):
                    await executor.ensure_plot_deps()
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
            f"{_memory_prompt(ctx)}"
            f"{_guidance_line(params)}"
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
        _remember(ctx, "报告", f"实验报告已生成（约 {len(experiment.report)} 字）")
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
