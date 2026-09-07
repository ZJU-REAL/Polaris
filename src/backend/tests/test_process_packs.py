"""流程包一期（#678，设计报告 §8.3）：加载/校验、extends 合并、物化保真对照。

保真是本 PR 的核心断言：base/experiment 包生成的计划必须与原 plan 函数
（navigator.experiment_plan）输出**相等**——两边任一漂移测试即红；
不选包的路径必须原样走 plan 函数（golden 保全铁律）。
"""

from pathlib import Path

import pytest

import app.agents.voyage  # noqa: F401  注册动作（包校验依赖注册表非空）
from app.agents.voyage.navigator import Navigator, experiment_plan
from app.models.voyage import VoyageRun
from app.schemas.experiment import ExperimentParams
from app.services import process_packs
from app.services.process_packs import (
    ProcessPackError,
    known_pack_names,
    load_pack,
    plan_from_pack,
)


def _run(params: dict | None) -> VoyageRun:
    return VoyageRun(
        kind="experiment",
        goal="实验验证：测试",
        status="planning",
        cursor=0,
        checkpoint={"params": params} if params is not None else None,
    )


# ---- 加载与校验 ----


def test_builtin_pack_loads():
    assert "base/experiment" in known_pack_names()
    pack = load_pack("base/experiment")
    assert pack.kind == "research-process"
    assert [p.id for p in pack.phases] == ["plan", "setup", "smoke", "iterate"]
    iterate = pack.phases[-1]
    assert iterate.loop is not None and iterate.loop.kind == "spiral"
    assert pack.guidance.strip()


def test_unknown_pack_rejected_with_known_list():
    with pytest.raises(ProcessPackError, match="base/experiment"):
        load_pack("no/such-pack")


def _write_pack(dir_: Path, filename: str, content: str) -> None:
    (dir_ / filename).write_text(content, encoding="utf-8")


def test_unknown_action_rejected_listing_known_actions(tmp_path, monkeypatch):
    monkeypatch.setattr(process_packs, "BUILTIN_PACKS_DIR", tmp_path)
    _write_pack(
        tmp_path,
        "bad.yaml",
        """
kind: research-process
name: bad/pack
phases:
  - id: p1
    actions: [experiment.teleport]
""",
    )
    with pytest.raises(ProcessPackError) as exc:
        load_pack("bad/pack")
    msg = str(exc.value)
    # 报错必须 actionable：指明未知 action 并列出已注册的 action 名
    assert "experiment.teleport" in msg
    assert "experiment.plan" in msg
    assert "p1" in msg


def test_bad_schema_and_bad_check_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(process_packs, "BUILTIN_PACKS_DIR", tmp_path)
    # kind 不对
    _write_pack(tmp_path, "wrongkind.yaml", "kind: pipeline\nname: x\n")
    with pytest.raises(ProcessPackError, match="schema"):
        process_packs._load_all()
    (tmp_path / "wrongkind.yaml").unlink()
    # 检查写法不认识
    _write_pack(
        tmp_path,
        "badcheck.yaml",
        """
kind: research-process
name: bad/check
phases:
  - id: p1
    actions: [experiment.plan]
    checks: ["telepathy:9"]
""",
    )
    with pytest.raises(ProcessPackError, match="telepathy"):
        load_pack("bad/check")


def test_duplicate_pack_name_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(process_packs, "BUILTIN_PACKS_DIR", tmp_path)
    body = "kind: research-process\nname: dup/pack\nphases: []\n"
    _write_pack(tmp_path, "a.yaml", body)
    _write_pack(tmp_path, "b.yaml", body)
    with pytest.raises(ProcessPackError, match="重名"):
        known_pack_names()


def test_extends_single_level_merge(tmp_path, monkeypatch):
    monkeypatch.setattr(process_packs, "BUILTIN_PACKS_DIR", tmp_path)
    _write_pack(
        tmp_path,
        "parent.yaml",
        """
kind: research-process
name: base/parent
phases:
  - id: a
    actions: [experiment.plan]
  - id: b
    actions: [experiment.setup]
guidance: 父包提示。
""",
    )
    _write_pack(
        tmp_path,
        "child.yaml",
        """
kind: research-process
name: sub/child
extends: base/parent
phases:
  - id: b
    actions: [experiment.smoke]
    checks: ["exit_code:0"]
  - id: c
    actions: [experiment.analyze]
guidance: 子包提示。
""",
    )
    pack = load_pack("sub/child")
    # 同 id 覆盖（保持父序）、新 id 追加
    assert [p.id for p in pack.phases] == ["a", "b", "c"]
    assert pack.phases[1].actions == ["experiment.smoke"]
    assert pack.phases[2].actions == ["experiment.analyze"]
    # guidance 拼接：父在前子在后
    assert pack.guidance == "父包提示。\n子包提示。"


def test_extends_unknown_parent_and_chain_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(process_packs, "BUILTIN_PACKS_DIR", tmp_path)
    _write_pack(
        tmp_path,
        "orphan.yaml",
        "kind: research-process\nname: sub/orphan\nextends: no/parent\nphases: []\n",
    )
    with pytest.raises(ProcessPackError, match="不存在"):
        load_pack("sub/orphan")
    _write_pack(
        tmp_path, "g.yaml", "kind: research-process\nname: g\nextends: mid\nphases: []\n"
    )
    _write_pack(
        tmp_path, "mid.yaml", "kind: research-process\nname: mid\nextends: root\nphases: []\n"
    )
    _write_pack(tmp_path, "root.yaml", "kind: research-process\nname: root\nphases: []\n")
    with pytest.raises(ProcessPackError, match="单层"):
        load_pack("g")


def test_inert_fields_accepted(tmp_path, monkeypatch):
    """gates / irreversible 进 schema 但惰性（deferral）：写了不报错、不产生行为。"""
    monkeypatch.setattr(process_packs, "BUILTIN_PACKS_DIR", tmp_path)
    _write_pack(
        tmp_path,
        "gated.yaml",
        """
kind: research-process
name: base/gated
phases:
  - id: p1
    actions: [experiment.setup]
gates:
  - {phase: p1, kind: irb_approval}
irreversible: [p1]
""",
    )
    pack = load_pack("base/gated")
    plan = plan_from_pack(pack, _run({}))
    # 惰性：声明的 gate 不落到计划节点上
    assert plan[0]["requires_gate"] is None


# ---- 物化保真：包生成的计划 == 原 plan 函数输出 ----

FIDELITY_PARAMS = [
    None,  # run=None（失败语义单测直接调计划模板的路径）
    {},
    {"confirm_budget": True},
    {"confirm_budget": False, "gpu_hint": "A100", "extra_notes": "备注"},
    {"confirm_budget": True, "eval_model": "gpt-x", "hf_mirror": True},
]


@pytest.mark.parametrize("params", FIDELITY_PARAMS)
def test_base_experiment_pack_matches_plan_function(params):
    run = _run(params) if params is not None else None
    pack = load_pack("base/experiment")
    assert plan_from_pack(pack, run) == experiment_plan(run)


# ---- Navigator 接入：不选包 = 原路径；选包 = 包生成 ----


@pytest.mark.asyncio
async def test_navigator_without_pack_uses_plan_function_verbatim():
    # 固定 kind 分支不触碰 LLM：llm=None 即可
    nav = Navigator(llm=None)  # type: ignore[arg-type]
    for params in ({}, {"confirm_budget": True}, {"process_pack": None}):
        run = _run(params)
        assert await nav.plan(run) == experiment_plan(run)


@pytest.mark.asyncio
async def test_navigator_with_pack_generates_equivalent_plan():
    nav = Navigator(llm=None)  # type: ignore[arg-type]
    for extra in ({}, {"confirm_budget": True}):
        run = _run({"process_pack": "base/experiment", **extra})
        assert await nav.plan(run) == experiment_plan(run)


@pytest.mark.asyncio
async def test_navigator_with_unknown_pack_raises_actionable():
    nav = Navigator(llm=None)  # type: ignore[arg-type]
    run = _run({"process_pack": "no/such"})
    with pytest.raises(ProcessPackError, match="base/experiment"):
        await nav.plan(run)


# ---- ExperimentParams.process_pack 校验 ----


def test_experiment_params_process_pack_validation():
    assert ExperimentParams().process_pack is None
    assert ExperimentParams(process_pack="base/experiment").process_pack == "base/experiment"
    with pytest.raises(ValueError, match="unknown process pack"):
        ExperimentParams(process_pack="no/such-pack")
