"""对照实验增强（feat/exp-autopilot）：Research Proposal 注入、conditions/eval_protocol
透传、baseline-vs-treatment 确定性对照分析。

这些是「从研究方案自动构建/分析对照实验」的核心新逻辑，用纯单元测试直接覆盖，
不依赖 DB/SSH（voyage e2e 由既有 test_experiments 覆盖）。
"""
from types import SimpleNamespace

from app.agents.voyage.actions_experiment import (
    _conditions_delta,
    _proposal_context,
    validate_plan,
)


def test_proposal_context_renders_for_proposal_depth():
    idea = SimpleNamespace(
        depth="proposal",
        research_type="benchmark",
        goal={
            "task": "检索增强数学推理评测",
            "question": "检索能否提升难题准确率？",
            "objectives": ["对比无检索与检索 harness"],
            "success_criteria": ["检索组显著优于 baseline"],
            "resources_needed": {"data": ["MATH-500", "NuminaMath 语料"]},
            "smoke_plan": {"baselines": ["no_retrieval"], "treatments": ["bm25", "dense"],
                           "datasets": ["MATH-500"], "metrics": ["accuracy"]},
        },
        evidence=[{"title": "Meta-Harness", "why": "检索 harness 提升推理"}],
    )
    ctx = _proposal_context(idea)
    assert "研究方案" in ctx
    assert "benchmark" in ctx
    assert "检索增强数学推理评测" in ctx
    assert "no_retrieval" in ctx and "bm25" in ctx  # smoke_plan 实验设计被注入
    assert "MATH-500" in ctx
    assert "Meta-Harness" in ctx


def test_proposal_context_empty_for_sketch():
    sketch = SimpleNamespace(depth="sketch", goal=None, research_type=None, evidence=None)
    assert _proposal_context(sketch) == ""


def test_validate_plan_passes_through_conditions():
    plan = validate_plan({
        "hypotheses": [{"text": "检索优于无检索", "status": "testing"}],
        "repro_strategy": "复现 Table 6",
        "steps": ["下载数据", "跑三条件", "对比"],
        "primary_metric": {"name": "accuracy", "direction": "maximize"},
        "budget_estimate": {"gpu_hours": 1},
        "conditions": [
            {"name": "no_retrieval", "role": "baseline", "description": "无检索"},
            {"name": "bm25", "role": "treatment", "description": "BM25"},
            {"name": "bad", "description": ""},  # role 缺失 → treatment
            {"role": "treatment"},  # 无 name → 丢弃
        ],
        "eval_protocol": {"dataset": "MATH-500", "metric": "accuracy", "n_examples": 130},
        "datasets": [{"name": "HuggingFaceH4/MATH-500", "purpose": "test"}],
    })
    conds = plan["conditions"]
    assert [c["name"] for c in conds] == ["no_retrieval", "bm25", "bad"]  # 无 name 的被丢
    assert conds[0]["role"] == "baseline" and conds[2]["role"] == "treatment"
    assert plan["eval_protocol"]["dataset"] == "MATH-500"
    assert plan["datasets"][0]["name"] == "HuggingFaceH4/MATH-500"


def test_validate_plan_backward_compatible_without_conditions():
    plan = validate_plan({
        "hypotheses": [{"text": "h", "status": "testing"}],
        "repro_strategy": "r",
        "steps": ["a", "b", "c"],
        "primary_metric": {"name": "loss", "direction": "minimize"},
        "budget_estimate": {"gpu_hours": 1},
    })
    assert "conditions" not in plan and "eval_protocol" not in plan


def test_conditions_delta_computes_baseline_vs_treatment():
    exp = SimpleNamespace(
        plan={"primary_metric": {"name": "accuracy", "direction": "maximize"},
              "conditions": [
                  {"name": "no_retrieval", "role": "baseline"},
                  {"name": "bm25", "role": "treatment"},
                  {"name": "dense", "role": "treatment"},
              ]},
        # 两个模型 × 三条件的 accuracy 序列（[{step,value}]）
        metrics={
            "accuracy/m1/no_retrieval": [{"step": 0, "value": 30.0}],
            "accuracy/m2/no_retrieval": [{"step": 0, "value": 40.0}],
            "accuracy/m1/bm25": [{"step": 0, "value": 34.0}],
            "accuracy/m2/bm25": [{"step": 0, "value": 46.0}],
            "accuracy/m1/dense": [{"step": 0, "value": 28.0}],
            "accuracy/m2/dense": [{"step": 0, "value": 41.0}],
            "ctx_chars/m1/bm25": [{"step": 0, "value": 6000.0}],  # 辅助指标：主指标族过滤会排除
        },
    )
    d = _conditions_delta(exp)
    assert d["baseline"] == "no_retrieval"
    assert d["scores"]["no_retrieval"] == 35.0  # (30+40)/2
    assert d["scores"]["bm25"] == 40.0  # (34+46)/2，ctx_chars 被主指标族过滤排除
    assert d["deltas_vs_baseline"]["bm25"] == 5.0  # 40-35：处理组优于 baseline
    # dense 低于 baseline（复现论文"dense retrieval 会回退"的现象）
    assert d["deltas_vs_baseline"]["dense"] < 0


def test_conditions_delta_none_without_conditions():
    exp = SimpleNamespace(plan={}, metrics={"accuracy": [{"step": 0, "value": 50}]})
    assert _conditions_delta(exp) is None
