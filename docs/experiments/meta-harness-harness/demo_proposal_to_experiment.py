"""Demonstrate the enhanced experiment module driving a controlled reproduction
from a Research Proposal, using the REAL model (claude-opus-4-8 via LiteLLM).

Runs from the worktree so it exercises the enhanced prompts + _proposal_context.
Step 1: proposal -> experiment.plan prompt -> plan JSON (must carry conditions).
Step 2: plan -> experiment.setup code prompt -> harness files (must download real
        data + evaluate every condition + emit per-condition POLARIS_METRIC).
"""
import asyncio
import json
from types import SimpleNamespace

import app.agents.voyage.actions_experiment as ax
from app.core.llm.base import Message
from app.core.llm.openai_compat import OpenAICompatProvider

BASE = "http://10.130.138.46:8010/v1"
KEY = "sk-_tONHdpuW_BT2hWqPC0cyA"
MODEL = "claude-opus-4-8"

# A Research Proposal extracted from Meta-Harness (arXiv 2603.28052) §4.2 Table 6,
# in the shape idea 2.0 深耕 produces (depth=proposal: goal + smoke_plan).
PROPOSAL = SimpleNamespace(
    title="检索增强对奥数推理的影响：无检索 vs BM25 vs 稠密检索（复现 Meta-Harness Table 6）",
    summary="给固定 LLM 加从解题语料检索相似题解的能力，评测其在难数学题上的 pass@1 是否提升",
    content="## 方法概述\n对固定模型 wrap 一层检索 harness，比较无检索 baseline 与 BM25/稠密检索。",
    depth="proposal",
    research_type="benchmark",
    goal={
        "task": "检索增强的奥数题求解评测",
        "question": "从大规模解题语料检索相似题解并加入上下文，能否提升固定 LLM 在难数学题上的准确率？",
        "objectives": ["复现 Meta-Harness §4.2 的核心对照：无检索 vs 检索 harness",
                       "量化不同检索策略（BM25/稠密）相对无检索 baseline 的准确率变化"],
        "success_criteria": ["得到每种检索策略相对 baseline 的 pass@1 delta 与结论",
                             "复现论文的关键现象（检索对数学推理并非总正向，可能回退）"],
        "scope": "推理评测（training-free），固定模型经 API 调用，CPU + 检索即可",
        "resources_needed": {"compute": "CPU + LLM API（无需 GPU 训练）",
                             "data": ["测试集：竞赛数学题（有可判定答案），如 MATH-500 hard 子集",
                                      "检索语料：竞赛题解数据集（如 NuminaMath 解题）"],
                             "time_weeks": 1},
        "smoke_plan": {
            "conditions": ["no_retrieval (baseline)", "bm25", "dense"],
            "baselines": ["无检索直接求解"],
            "treatments": ["BM25 检索 top-k 题解注入上下文", "稠密向量检索 top-k 题解注入上下文"],
            "datasets": {"test": "MATH-500 level-5", "corpus": "NuminaMath 竞赛题解"},
            "metric": "pass@1 accuracy（答案判定）", "k": 3, "models": "1-2 个 API 模型",
        },
        "key_concepts": ["retrieval-augmented reasoning", "harness engineering", "pass@1"],
    },
    evidence=[{"title": "Meta-Harness: End-to-End Optimization of Model Harnesses",
               "why": "§4.2/Table 6 的检索增强数学推理对照实验，本方案复现其核心对比"}],
)


async def main():
    prov = OpenAICompatProvider(BASE, KEY)

    # --- Step 1: proposal -> plan ---
    plan_user = (
        f"想法标题：{PROPOSAL.title}\n"
        f"想法概述：{PROPOSAL.summary}\n"
        f"想法详情：\n{PROPOSAL.content}\n"
        f"{ax._proposal_context(PROPOSAL)}\n"
        f"相关 wiki 摘要：\n（略）\n\n"
        f"预算约束：{json.dumps({'max_hours': 3, 'max_runs': 3})}\nGPU 提示：无 GPU，走 LLM API"
    )
    print("=== proposal 注入进 plan prompt 的上下文片段 ===")
    print(ax._proposal_context(PROPOSAL)[:600])
    r1 = await prov.complete(
        [Message(role="system", content=ax.PLAN_SYSTEM_PROMPT), Message(role="user", content=plan_user)],
        model=MODEL, max_tokens=4000,
    )
    plan = ax.validate_plan(json.loads(_strip(r1.content)))
    print("\n=== Step1 生成的实验计划 ===")
    print("conditions:", [(c["name"], c["role"]) for c in plan.get("conditions", [])])
    print("eval_protocol:", json.dumps(plan.get("eval_protocol"), ensure_ascii=False))
    print("datasets:", json.dumps(plan.get("datasets"), ensure_ascii=False)[:300])
    print("primary_metric:", plan.get("primary_metric"))
    assert plan.get("conditions"), "计划未产出对照条件！"
    assert any(c["role"] == "baseline" for c in plan["conditions"]), "缺 baseline"

    # --- Step 2: plan -> code ---
    ctx = SimpleNamespace(checkpoint={"params": {"eval_model": "dashscope/qwen-turbo", "hf_mirror": True}})
    code_system = ax._prompt_with_context(ax.CODE_SYSTEM_PROMPT, ctx)
    code_user = f"实验计划：{json.dumps(plan, ensure_ascii=False)[:8000]}\n预算：{json.dumps({'max_hours': 3})}"
    r2 = await prov.complete(
        [Message(role="system", content=code_system), Message(role="user", content=code_user)],
        model=MODEL, max_tokens=8000,
    )
    files = ax.validate_files(json.loads(_strip(r2.content)))
    print("\n=== Step2 生成的实验代码文件 ===")
    print("files:", list(files.keys()))
    blob = "\n".join(files.values())
    checks = {
        "读 llm_config.json（不硬编码 key）": "llm_config.json" in blob,
        "下载真实数据集(load_dataset)": "load_dataset" in blob or "datasets" in files.get("requirements.txt", ""),
        "三个条件对照": sum(x in blob for x in ["no_retrieval", "bm25", "dense"]) >= 2,
        "逐条件 POLARIS_METRIC": "POLARIS_METRIC" in blob,
        "run.sh 支持 --smoke": "--smoke" in files.get("run.sh", ""),
    }
    for k, v in checks.items():
        print(f"  [{'✓' if v else '✗'}] {k}")
    print("\n=== run.sh 预览 ===\n" + files.get("run.sh", "")[:500])


def _strip(t: str) -> str:
    t = t.strip()
    if t.startswith("```"):
        t = t.split("```", 2)[1]
        t = t[t.find("{"):]
    i, j = t.find("{"), t.rfind("}")
    return t[i:j + 1] if i >= 0 else t


asyncio.run(main())
