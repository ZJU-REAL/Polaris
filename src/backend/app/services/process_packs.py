"""流程包（research-process）一期：科研流程从代码降级为数据（设计报告 §8.3，#678）。

学科流程差异集中在四维——循环拓扑、硬闸门位置、不可逆性、确定性/判断性配比——
流程包把流程定义收进 YAML：phases（引用注册表里的 action）+ checks/rubrics +
loop 拓扑 + guidance 软知识。本期范围（其余按 deferral 决策明确不做）：

- 只读内置包目录（``app/packs/``，file-over-app）；用户数据目录的包后续接入；
- ``gates`` / ``irreversible`` 字段进 schema 但**惰性**：IRB 类学科现实需要表达力，
  schema 先收下占位，引擎本期不消费（deferral 决策，见 #678）；预算闸门仍沿用
  params 开关（与原 plan 函数逐字节一致）；
- loop 只做顺序展开第 1 轮（后续轮由 plan_edit.SIGNAL_TABLES 确定性追加）；
  fanout/funnel 拓扑等引擎支持后再消费 ``loop.kind``；
- 不选包 = navigator 原 plan 函数原路径，行为逐字节不变（golden 保全铁律）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.models.voyage import VoyageRun

# 内置包目录（随 app 包分发，见 pyproject [tool.setuptools.package-data]）
BUILTIN_PACKS_DIR = Path(__file__).resolve().parents[1] / "packs"


class ProcessPackError(Exception):
    """流程包非法（YAML 解析失败 / schema 不符 / 引用未注册的 action 等）。

    message 必须 actionable：指明哪个文件/哪个 phase、期望什么——它会原样出现在
    创建实验的 422 响应与任务失败诊断里。
    """


class PackLoop(BaseModel):
    """phase 的循环拓扑声明。本期只作标注：引擎仍顺序展开第 1 轮，
    后续轮由确定性分支表追加；fanout（扇出并行）/funnel（漏斗收敛）待引擎支持。"""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["fanout", "spiral", "funnel"]
    exit_check: str | None = None
    max_parallel: int | None = Field(default=None, ge=1)


class PackPhase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    # 引用动作注册表里的 action 名（app/agents/voyage/actions.py register()）
    actions: list[str] = []
    # 确定性校验（Sextant 检查项的字符串写法，见 _parse_check）；None = 缺省 no_error
    checks: list[str] | None = None
    # LLM 评审 rubric（追加为 llm_rubric 检查项）
    rubrics: list[str] | None = None
    loop: PackLoop | None = None


class ProcessPack(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["research-process"]
    name: str = Field(min_length=1)
    # 单层继承：phases 追加/覆盖同 id、guidance 拼接（父包自身不得再 extends）
    extends: str | None = None
    phases: list[PackPhase] = []
    # 软知识：后续渲染进 Navigator 规划提示（自由档投影）；严格档本期不消费
    guidance: str = ""
    # ---- 惰性字段（deferral，#678）----
    # 硬闸门与不可逆声明进 schema 占位（IRB/湿实验类学科需要这份表达力），
    # 引擎本期不消费：写了不报错、也不产生任何行为。
    gates: list[dict[str, Any]] = []
    irreversible: list[str] = []


# ---- 检查项字符串语法 ----
# YAML 里 checks 用紧凑字符串（"no_error" / "exit_code:0" / "artifact_exists:<键>"），
# 解析成 Sextant 检查项 dict（app/agents/voyage/checks.py）。只开放包目前用得上的
# 确定性检查；新增 kind 时同步扩这里，别让包写出引擎不认识的检查。


def _parse_check(spec: str) -> dict[str, Any]:
    kind, _, arg = spec.partition(":")
    if kind == "no_error" and not arg:
        return {"kind": "no_error"}
    if kind == "exit_code":
        try:
            return {"kind": "exit_code", "value": int(arg or "0")}
        except ValueError as e:
            raise ProcessPackError(f"exit_code 检查的期望值必须是整数：{spec!r}") from e
    if kind == "artifact_exists" and arg:
        return {"kind": "artifact_exists", "key": arg}
    raise ProcessPackError(
        f"未知的检查写法 {spec!r}；支持：no_error / exit_code:<int> / artifact_exists:<键>"
    )


def _phase_checks(phase: PackPhase) -> list[dict[str, Any]]:
    """phase 的检查项：显式 checks（+ rubrics）；都没写则缺省机械验收 no_error。"""
    checks = [_parse_check(spec) for spec in phase.checks or []]
    checks += [{"kind": "llm_rubric", "rubric": r} for r in phase.rubrics or []]
    return checks or [{"kind": "no_error"}]


# ---- 加载与校验 ----


def _read_pack_file(path: Path) -> ProcessPack:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise ProcessPackError(f"流程包 {path.name} YAML 解析失败：{e}") from e
    if not isinstance(data, dict):
        raise ProcessPackError(f"流程包 {path.name} 顶层必须是映射（mapping）")
    try:
        return ProcessPack.model_validate(data)
    except ValidationError as e:
        raise ProcessPackError(f"流程包 {path.name} schema 校验失败：{e}") from e


def _load_all() -> dict[str, ProcessPack]:
    """读入包目录下全部 YAML，按 name 索引。

    不缓存：包是磁盘上的数据（file-over-app），目录很小，每次现读省掉
    「改了包文件测试/进程还拿旧缓存」一整类坑。
    """
    packs: dict[str, ProcessPack] = {}
    if not BUILTIN_PACKS_DIR.is_dir():
        return packs
    for path in sorted(BUILTIN_PACKS_DIR.rglob("*.yaml")):
        pack = _read_pack_file(path)
        if pack.name in packs:
            raise ProcessPackError(f"流程包重名 {pack.name!r}（{path.name}）")
        packs[pack.name] = pack
    return packs


def known_pack_names() -> frozenset[str]:
    """可选的流程包名集合（ExperimentParams.process_pack 校验用）。"""
    return frozenset(_load_all())


def _merge(parent: ProcessPack, child: ProcessPack) -> ProcessPack:
    """单层继承合并：phases 同 id 覆盖（保持父序）、新 id 追加；guidance 拼接。

    gates/irreversible 是惰性字段，不做合并语义（取子包自己的声明），
    等启用拦截时再定合并规则——现在定了也没人消费，白背兼容包袱。
    """
    phases = list(parent.phases)
    index = {p.id: i for i, p in enumerate(phases)}
    for phase in child.phases:
        if phase.id in index:
            phases[index[phase.id]] = phase
        else:
            phases.append(phase)
    guidance = "\n".join(g for g in (parent.guidance, child.guidance) if g)
    return child.model_copy(update={"phases": phases, "guidance": guidance})


def _validate_pack(pack: ProcessPack) -> None:
    """语义校验：action 必须在注册表内（报错列出已知 action 名）、检查写法合法。"""
    # 延迟导入：注册表随 app.agents.voyage 包的 import 副作用填充，
    # 这里主动 import 一次保证独立调用（测试/脚本）时注册表非空
    import app.agents.voyage  # noqa: F401
    from app.agents.voyage.actions import known_actions

    known = known_actions()
    for phase in pack.phases:
        for action in phase.actions:
            if action not in known:
                raise ProcessPackError(
                    f"流程包 {pack.name!r} 的 phase {phase.id!r} 引用了未注册的 "
                    f"action {action!r}；已注册：{', '.join(sorted(known))}"
                )
        _phase_checks(phase)  # 检查写法非法在加载期就报，别拖到生成计划时


def load_pack(name: str) -> ProcessPack:
    """按名加载流程包（含 extends 解析与语义校验）；非法/未知抛 ProcessPackError。"""
    packs = _load_all()
    pack = packs.get(name)
    if pack is None:
        raise ProcessPackError(
            f"未知流程包 {name!r}；可选：{', '.join(sorted(packs)) or '（无）'}"
        )
    if pack.extends is not None:
        parent = packs.get(pack.extends)
        if parent is None:
            raise ProcessPackError(
                f"流程包 {name!r} 继承的 {pack.extends!r} 不存在；"
                f"可选：{', '.join(sorted(packs))}"
            )
        if parent.extends is not None:
            raise ProcessPackError(
                f"流程包 {name!r} 继承的 {pack.extends!r} 自身还有 extends——只支持单层继承"
            )
        pack = _merge(parent, pack)
    _validate_pack(pack)
    return pack


# ---- 包 → 计划生成 ----

# action → 计划节点元数据（标题 / 验收 / 失败语义）。这些是动作自身的呈现与失败
# 语义，属于「动作注册侧」的元数据——组件注册表（Runner v2 manifest 方向）成型前
# 先表驱动收在这里，包 YAML 只写流程拓扑。与 navigator 原 plan 函数的保真由
# tests/test_process_packs.py 的逐 params 对照断言守护：表漂移测试即红。
# round_param=True 的动作在 loop phase 里带 {"round": N} 参数、标题按轮次格式化。
_STEP_TEMPLATES: dict[str, dict[str, Any]] = {
    "experiment.plan": {
        "title": "实验计划（LLM）",
        "acceptance": "plan JSON（含 primary_metric）已通过严格校验并写入 Experiment.plan",
    },
    "experiment.setup": {
        "title": "建环境（SSH + 代码生成）",
        "acceptance": "远端 workdir 就绪、代码文件已写入、venv 依赖安装成功",
    },
    "experiment.smoke": {
        "title": "冒烟测试",
        "acceptance": "run.sh --smoke 退出码为 0",
        # 动作内部已有 LLM 修复循环：步骤级不重试也不交 AI 重排（原 plan 函数语义）
        "on_failure": "fail",
        "budget": {"max_attempts": 1},
    },
    "experiment.run": {
        "title": "第 {round} 轮运行",
        "acceptance": "本轮运行已结束并解析主指标（失败轮交由分析步骤诊断）",
        # 运行级失败 = 预算超时/基础设施故障，盲目重跑烧算力，诚实硬停
        "on_failure": "fail",
        "budget": {"max_attempts": 1},
        "round_param": True,
    },
    "experiment.analyze": {
        "title": "第 {round} 轮分析",
        "acceptance": "reflection 已落库并给出继续/收束判定",
        "round_param": True,
    },
}


def plan_from_pack(pack: ProcessPack, run: VoyageRun | None) -> list[dict[str, Any]]:
    """把流程包展开为 voyage 计划步骤（phases 顺序展开，action 逐一映射节点）。

    与 navigator 的原 plan 函数**等价**是硬约束（物化保真）：base/experiment 包
    生成的计划必须与 experiment_plan() 输出一致（测试逐 params 断言）。

    loop phase 本期只展开第 1 轮（round=1）：后续轮由动作的 plan_signal 走
    plan_edit.SIGNAL_TABLES 确定性追加，与现状同一套机制；fanout/funnel 等
    引擎支持后再按 loop.kind 分派展开方式。
    """
    checkpoint = (run.checkpoint if run is not None else None) or {}
    params = checkpoint.get("params") or {}
    steps: list[dict[str, Any]] = []
    for phase in pack.phases:
        checks = _phase_checks(phase)
        in_loop = phase.loop is not None
        for action in phase.actions:
            tpl = _STEP_TEMPLATES.get(action) or {}
            title = str(tpl.get("title") or action)
            node_params: dict[str, Any] = {}
            if in_loop and tpl.get("round_param"):
                node_params["round"] = 1
                title = title.format(round=1)
            node: dict[str, Any] = {
                "title": title,
                "action": action,
                "params": node_params,
                "acceptance": tpl.get("acceptance"),
                "checks": [dict(c) for c in checks],  # 每节点独立副本，防共享可变态
                "requires_gate": None,
            }
            if tpl.get("on_failure"):
                node["on_failure"] = tpl["on_failure"]
            if tpl.get("budget"):
                node["budget"] = dict(tpl["budget"])
            steps.append(node)
    # gates 字段本期惰性（deferral）：预算闸门沿用 params 开关（#626 默认不拦），
    # 与原 plan 函数逐字节一致；待 gates 启用后改由包声明闸门位置。
    if bool(params.get("confirm_budget")):
        for node in steps:
            if node["action"] == "experiment.setup":
                node["requires_gate"] = "compute_budget"
    return steps
