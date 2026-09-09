"""计划模板注册表（#743）：完备性、分派保真、严格查找可诊断。

注册表替换 Navigator.plan 里的 if 链是纯分派重构：每个已注册 kind 必须分派到
原计划函数（输出相等）；未注册 kind 仍落 LLM 自由规划兜底（skills v1 的
custom 靠它，不是 unknown 错误）；严格入口 get_plan_builder 的报错必须带
全量 kind 清单（对齐 runners.registry 的可诊断风格）。
"""

import pytest

import app.agents.voyage  # noqa: F401  注册动作（计划模板随包 import 挂载）
from app.agents.voyage import navigator
from app.agents.voyage.navigator import (
    Navigator,
    UnknownPlanKindError,
    daily_feed_plan,
    demo_plan,
    discovery_plan,
    get_plan_builder,
    known_plan_kinds,
    proposal_replan,
    register_plan,
    wiki_plan,
)
from app.models.voyage import VoyageRun

# 固定计划模板的 kind 全集：新增 kind 必须同步进这里——否则要么模板没挂上
# 注册表（掉进 LLM 兜底），要么测试没跟上，两种都该红。
EXPECTED_KINDS = frozenset(
    {
        "demo",
        "wiki_bootstrap",
        "wiki_ingest",
        "daily_feed_sync",
        "idea_forge",
        "idea_review",
        "idea_proposal",
        "experiment",
        "paper_writing",
        "paper_review",
        "presentation",
        "discovery",
    }
)


def _run(kind: str, params: dict | None = None) -> VoyageRun:
    return VoyageRun(
        kind=kind,
        goal="测试目标",
        status="planning",
        cursor=0,
        checkpoint={"params": params or {}},
    )


def test_registry_covers_all_fixed_kinds():
    assert known_plan_kinds() == EXPECTED_KINDS


def test_wiki_kinds_share_one_builder():
    # 一个模板可服务多个 kind（叠放装饰器），两个 wiki kind 指向同一函数
    assert get_plan_builder("wiki_bootstrap") is wiki_plan
    assert get_plan_builder("wiki_ingest") is wiki_plan


def test_unknown_kind_error_lists_known_kinds():
    with pytest.raises(UnknownPlanKindError) as exc:
        get_plan_builder("no_such_kind")
    msg = str(exc.value)
    assert "no_such_kind" in msg
    for kind in ("experiment", "discovery", "wiki_bootstrap"):
        assert kind in msg


def test_duplicate_registration_rejected():
    # 静默覆盖会把分派变成 import 顺序问题——重名必须当场报错
    with pytest.raises(ValueError, match="already registered"):
        register_plan("demo")(demo_plan)


@pytest.mark.asyncio
async def test_plan_dispatches_to_original_functions():
    # 固定 kind 分派不触碰 LLM：llm=None 即可；输出必须与直调原函数相等
    nav = Navigator(llm=None)  # type: ignore[arg-type]
    assert await nav.plan(_run("demo")) == demo_plan(_run("demo"))
    assert await nav.plan(_run("wiki_ingest")) == wiki_plan(_run("wiki_ingest"))
    assert await nav.plan(_run("daily_feed_sync")) == daily_feed_plan(_run("daily_feed_sync"))
    assert await nav.plan(_run("discovery")) == discovery_plan(_run("discovery"))


def test_replan_registry_holds_only_proposal():
    # 唯一的确定性重规划：idea_proposal；其余 kind 走 LLM 重规划
    assert {"idea_proposal": proposal_replan} == navigator._REPLAN_BUILDERS


@pytest.mark.asyncio
async def test_replan_dispatches_deterministic_proposal():
    nav = Navigator(llm=None)  # type: ignore[arg-type]
    run = _run("idea_proposal")
    failed = {"action": "proposal.design", "title": "研究方案设计", "params": {}}
    diagnosis = "NEEDS_DIFFERENTIATION: 与库内工作重合"
    assert await nav.replan(run, failed, diagnosis) == proposal_replan(run, failed, diagnosis)
