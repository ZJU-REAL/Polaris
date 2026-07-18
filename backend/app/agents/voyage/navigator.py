"""Navigator（领航 · planning）：把目标分解为步骤列表；执行失败时依据诊断重规划。

- 输出严格 JSON（{"steps": [...]}），做 schema 校验，解析失败重试 2 次；
- ``demo`` kind 用固定三步计划（不依赖 LLM 规划质量），第 2 步声明
  ``requires_gate="compute_budget"`` 以演示人在环闸门。
"""

import json
from typing import Any

from app.agents.voyage.actions import known_actions
from app.agents.voyage.checks import validate_checks
from app.agents.voyage.plan_edit import experiment_round_nodes, validate_plan_edit
from app.agents.voyage.skillset import skill_workflows
from app.core.llm.base import Message
from app.core.llm.router import LLMRouter
from app.models.voyage import VoyageRun

_MAX_ATTEMPTS = 3  # 首次 + 重试 2 次

# 产出文本、走遗留 LLM/内容判定的基础动作（其余动作缺省补 no_error 机械验收）
_CONTENT_ACTIONS = frozenset({"llm.complete", "sleep", "artifact.write"})

PLAN_SYSTEM_PROMPT = """\
你是 Navigator，负责把科研目标分解为可执行的步骤计划。
只输出一个 JSON 对象，不要输出任何其他文字或 Markdown 代码块，格式：
{"steps": [{"title": "步骤标题", "action": "动作名", "params": {}, \
"acceptance": "验收标准", "requires_gate": null}]}
约束：
- action 只能取：%(actions)s
- llm.complete 的 params 需含 stage 与 prompt（prompt 可用 {goal} 模板变量）
- artifact.write 的 params 需含 name 与 content
- 需要人工审批的步骤把 requires_gate 设为闸门类型（如 "compute_budget"），否则为 null
- 步骤控制在 5 个以内
"""

REPLAN_SYSTEM_PROMPT = """\
你是 Navigator，负责在某个步骤验证失败后重新规划剩余步骤。
只输出一个 JSON 对象，不要输出任何其他文字或 Markdown 代码块，格式：
{"steps": [{"title": "步骤标题", "action": "动作名", "params": {}, \
"acceptance": "验收标准", "requires_gate": null}]}
约束与初次规划相同；action 只能取：%(actions)s。
输出的 steps 将替换原计划中失败步骤起的剩余部分。
"""

PLAN_EDIT_SYSTEM_PROMPT = """\
POLARIS_PLAN_EDIT
你是 Navigator，负责在某个步骤失败后对任务计划做增量编辑（docs/voyage-loop.md §5.3）。
只输出一个 JSON 对象，不要输出任何其他文字或 Markdown 代码块，两种形式二选一：
1. 编辑计划：{"reason": "一句话说明", "edits": [
   {"op": "add_nodes", "insert_after": "步骤id 或 null（null=插到失败位置）",
    "nodes": [{"title": "...", "action": "动作名", "params": {}, "acceptance": "验收标准",
    "requires_gate": null}]},
   {"op": "update_node", "step_id": "...", "params": {}},
   {"op": "obsolete_nodes", "step_ids": ["..."], "reason": "..."}]}
2. 建议收束：{"finish": true, "reason": "..."}
约束：
- action 只能取：%(actions)s
- 每个新节点必须带 acceptance；单次编辑新增节点 ≤ 8 个
- 只能引用未完成的步骤 id；已通过/已作废的步骤不可编辑
- 失败步骤会在编辑生效后自动作废，你只需给出替代/补充步骤
"""


class NavigatorError(Exception):
    """规划失败（LLM 连续产出非法 JSON 等）。"""


# 固定计划模板的 kind（不靠 LLM 自由规划）
WIKI_KINDS = ("wiki_bootstrap", "wiki_ingest")
IDEA_KINDS = ("idea_forge", "idea_review", "idea_proposal")


def wiki_plan(run: VoyageRun) -> list[dict[str, Any]]:
    """文献 ingest 固定七步计划（docs/api-m2.md §7）；knobs 从 checkpoint.params 读。"""
    steps = [
        ("检索候选（arXiv）", "wiki.search_candidates", "候选论文已入库"),
        ("参考文献扩展（Semantic Scholar）", "wiki.snowball", "参考文献扩展完成"),
        ("相关性打分（LLM）", "wiki.score_relevance", "候选论文已全部打分或排除"),
        ("下载 PDF + 抽全文", "wiki.fetch_extract", "top-N 论文全文就绪（失败降级摘要）"),
        ("Librarian 编译 wiki 页", "wiki.compile", "top-N 论文已生成中文 wiki markdown"),
        ("概念上链 + embedding", "wiki.link_concepts", "双链概念已 upsert 并关联论文"),
        ("记录同步进度", "wiki.update_watermark", "项目 ingest_state 已更新"),
    ]
    return [
        {
            "title": title,
            "action": action,
            "params": {},
            "acceptance": acceptance,
            "checks": [{"kind": "no_error"}],
            "requires_gate": None,
        }
        for title, action, acceptance in steps
    ]


def forge_plan(run: VoyageRun) -> list[dict[str, Any]]:
    """idea_forge 固定七步计划（docs/api-m3.md §1 + docs/api-idea2.md §1 信号升级）。"""
    steps = [
        ("读取知识库上下文", "forge.read_context", "compiled wiki 页与概念已汇总为上下文"),
        ("信号采集（组合空白/趋势/局限）", "forge.collect_signals", "启用的信号源已完成采集"),
        ("研究空白综合", "forge.gap_analysis", "已产出带信号来源的研究空白清单"),
        ("生成候选 idea（LLM）", "forge.generate", "已生成 num_ideas 个候选 idea"),
        ("四维打分（LLM 逐条）", "forge.score", "候选 idea 已获得四维评分与理由"),
        ("语义去重（embedding + rerank）", "forge.dedup", "重复候选已丢弃并记录"),
        ("入库候选池", "forge.persist", "存活候选已入库（status=candidate，depth=sketch）"),
    ]
    return [
        {
            "title": title,
            "action": action,
            "params": {},
            "acceptance": acceptance,
            "checks": [{"kind": "no_error"}],
            "requires_gate": None,
        }
        for title, action, acceptance in steps
    ]


def _proposal_step(
    title: str,
    action: str,
    acceptance: str,
    *,
    requires_gate: str | None = None,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "title": title,
        "action": action,
        "params": params or {},
        "acceptance": acceptance,
        "checks": [{"kind": "no_error"}],
        "requires_gate": requires_gate,
    }


def _proposal_body_steps() -> list[dict[str, Any]]:
    """阶段二/三固定骨架（docs/api-idea2.md §5/§6）。"""
    return [
        _proposal_step(
            "相关工作定位", "proposal.related_work", "覆盖全部 grounding 论文并给出差异对比"
        ),
        _proposal_step(
            "研究方案设计", "proposal.design", "按 research_type 模板完成设计且给出依据"
        ),
        _proposal_step(
            "实验与评估计划", "proposal.experiments", "含主指标、资源核对与最小验证实验"
        ),
        _proposal_step(
            "新颖性核查", "proposal.novelty_check", "库内+外部相似工作已逐条差异论证且判定 novel"
        ),
        _proposal_step("风险与备选方案", "proposal.risks", "至少 2 条风险且各配缓解/备选"),
        _proposal_step("汇编入库", "proposal.assemble", "Research Proposal 已入库为 idea"),
        _proposal_step(
            "评审与修订", "proposal.review_revise", "四维专职评审完成，终评分数与遗留问题已落库"
        ),
    ]


def proposal_plan(run: VoyageRun) -> list[dict[str, Any]]:
    """idea_proposal 固定计划（docs/api-idea2.md）：
    目标构建 →（confirm_goal 时 idea_goal 闸门 + 修订）→ 方案深耕 → 评审修订。
    """
    params = (run.checkpoint or {}).get("params") or {}
    knobs = params.get("knobs") if isinstance(params.get("knobs"), dict) else {}
    confirm_goal = knobs.get("confirm_goal", True)
    steps = [
        _proposal_step(
            "目标构建（文献探索）", "goal.explore", "goal 已通过结构校验（grounding/可检验目标）"
        )
    ]
    if confirm_goal:
        steps.append(
            _proposal_step(
                "目标确认（人工审批）",
                "goal.refine",
                "研究目标已确认（如有审批意见则已并入）",
                requires_gate="idea_goal",
            )
        )
    return steps + _proposal_body_steps()


# 确定性重规划的诊断前缀（proposal.novelty_check 产出）
_DIAG_NEEDS_DIFF = "NEEDS_DIFFERENTIATION"
_DIAG_DUPLICATE = "DUPLICATE"


def proposal_replan(
    run: VoyageRun, failed_step: dict[str, Any], diagnosis: str
) -> list[dict[str, Any]]:
    """idea_proposal 确定性重规划（不经 LLM，docs/api-idea2.md §5）：

    - DUPLICATE → 插入 idea_pivot 闸门的 goal.refine，再从 design 起重跑；
    - NEEDS_DIFFERENTIATION → 从 design 起重跑（带诊断回炉设计）；
    - 其余失败 → 从失败步骤起原样重跑（失败步骤带上诊断）。
    """
    body = _proposal_body_steps()

    def tail_from(action: str, diag: str | None) -> list[dict[str, Any]]:
        index = next((i for i, s in enumerate(body) if s["action"] == action), 0)
        tail = [dict(s, params=dict(s["params"])) for s in body[index:]]
        if diag:
            tail[0]["params"]["diagnosis"] = diag[:2000]
        return tail

    if diagnosis.startswith(_DIAG_DUPLICATE):
        pivot = _proposal_step(
            "方向调整确认（人工审批）",
            "goal.refine",
            "研究方向已按人工意见调整并通过结构校验",
            requires_gate="idea_pivot",
            params={"reason": "duplicate"},
        )
        return [pivot, *tail_from("proposal.design", f"方向已调整（原判定：{diagnosis[:500]}）")]
    if diagnosis.startswith(_DIAG_NEEDS_DIFF):
        return tail_from("proposal.design", diagnosis)

    failed_action = str(failed_step.get("action") or "")
    if failed_action == "goal.explore":
        steps = proposal_plan(run)
        steps[0]["params"] = dict(steps[0]["params"]) | {"diagnosis": diagnosis[:2000]}
        return steps
    if failed_action == "goal.refine":
        # 修订失败：保留闸门语义重跑该步（闸门已批准过，engine 会直接放行）
        retry = dict(failed_step, params=dict(failed_step.get("params") or {}))
        retry["params"]["diagnosis"] = diagnosis[:2000]
        return [retry, *_proposal_body_steps()]
    if any(s["action"] == failed_action for s in body):
        return tail_from(failed_action, diagnosis)
    return tail_from(body[0]["action"], diagnosis)


def review_plan(run: VoyageRun) -> list[dict[str, Any]]:
    """idea_review（辩论锦标赛）启动计划（docs/api-m3.md §3 + docs/voyage-loop.md §7）：
    配对 → 汇总；中间的 N 场辩论由 review.pair 的 plan_signal 按对局数展开成
    N 个 review.match 节点插入两者之间（引擎可逐场查预算，超限走降级收尾）。
    """
    steps = [
        ("配对（Swiss：按 Elo 相邻配对）", "review.pair", "参与 idea 已配对并置 under_review"),
        ("锦标赛汇总", "review.summarize", "赛果已汇总并写入活动流"),
    ]
    return [
        {
            "title": title,
            "action": action,
            "params": {},
            "acceptance": acceptance,
            "checks": [{"kind": "no_error"}],
            "requires_gate": None,
            # 汇总是廉价收尾：预算耗尽也放行，别让辩论白跑（docs/voyage-loop.md §5.4）
            "wrapup": action == "review.summarize",
        }
        for title, action, acceptance in steps
    ]


def experiment_plan(run: VoyageRun) -> list[dict[str, Any]]:
    """experiment 启动计划（docs/voyage-loop.md §7，mode=loop）：
    计划 →（compute_budget 闸门）建环境 → 冒烟 → 第 1 轮运行 → 第 1 轮分析。
    后续轮次由 experiment.analyze 的 plan_signal 走确定性分支表动态追加
    （improve/debug → 下一轮 run+analyze；终止 → figures+report 收尾）。

    失败语义按节点分派：plan/setup/analyze/figures/report 失败走 loop 回灌
    （原地重试 → AI 计划调整）；smoke 保留 on_failure="fail"——动作内部已有
    LLM 修复循环（MAX_SMOKE_FIXES），修完仍失败说明代码根本性不可用，诚实硬停
    且 max_attempts=1 防引擎级重跑整个修复循环。
    """
    steps = [
        (
            "实验计划（LLM）",
            "experiment.plan",
            "plan JSON（含 primary_metric）已通过严格校验并写入 Experiment.plan",
            None,
        ),
        (
            "建环境（SSH + 代码生成）",
            "experiment.setup",
            "远端 workdir 就绪、代码文件已写入、venv 依赖安装成功",
            "compute_budget",
        ),
        ("冒烟测试", "experiment.smoke", "run.sh --smoke 退出码为 0", None),
    ]
    head = []
    for title, action, acceptance, gate in steps:
        node: dict[str, Any] = {
            "title": title,
            "action": action,
            "params": {},
            "acceptance": acceptance,
            # 冒烟测试用退出码机械判定（原 Sextant 硬编码逻辑，docs/voyage-loop.md §6）
            "checks": [{"kind": "exit_code", "value": 0}]
            if action == "experiment.smoke"
            else [{"kind": "no_error"}],
            "requires_gate": gate,
        }
        if action == "experiment.smoke":
            node["on_failure"] = "fail"
            node["budget"] = {"max_attempts": 1}
        head.append(node)
    return head + experiment_round_nodes(1)


# voyage 级完成标准（docs/voyage-loop.md §5.4）：engine 在规划时写入 run.done_criteria。
# experiment 防"过早宣告完成"：迭代必须有明确终止判定、报告必须已生成
_DONE_CRITERIA_BY_KIND: dict[str, dict[str, Any]] = {
    "experiment": {
        "checks": [
            {"kind": "artifact_exists", "key": "iterate.stopped_reason"},
            {"kind": "artifact_exists", "key": "report_done"},
        ]
    },
}


def done_criteria_for_kind(kind: str) -> dict[str, Any] | None:
    return _DONE_CRITERIA_BY_KIND.get(kind)


_WRITING_SECTION_TITLES = {
    "abstract": "Abstract",
    "introduction": "Introduction",
    "method": "Method",
    "experimental_setup": "Experimental Setup",
    "results": "Results",
    "conclusion": "Conclusion",
}


def writing_plan(run: VoyageRun) -> list[dict[str, Any]]:
    """paper_writing 固定计划（docs/api-m5-b.md §5）：
    分节固定顺序撰写 →（中期编译）→ Related Work（候选集内选引）→ 终编译。
    固定管线不重规划：所有步骤 on_failure="fail"；终编译 ok 才算 done。
    """
    params = (run.checkpoint or {}).get("params") or {}
    sections = [s for s in params.get("sections") or [] if s in _WRITING_SECTION_TITLES]
    related = bool(params.get("related_work"))
    steps: list[dict[str, Any]] = [
        {
            "title": f"撰写 {_WRITING_SECTION_TITLES[s]}",
            "action": "writing.section",
            "params": {"section": s},
            "acceptance": f"{s} 节已通过静态校验并写入稿件文件",
            "checks": [{"kind": "no_error"}],
            "requires_gate": None,
            "on_failure": "fail",
        }
        for s in sections
    ]
    if related:
        steps.append(
            {
                "title": "中期编译",
                "action": "writing.compile",
                "params": {"phase": "mid"},
                "acceptance": "编译已执行并记录诊断（中期编译不阻塞）",
                "checks": [{"kind": "no_error"}],
                "requires_gate": None,
                "on_failure": "fail",
            }
        )
        steps.append(
            {
                "title": "撰写 Related Work（候选集内选引）",
                "action": "writing.related_work",
                "params": {},
                "acceptance": "related_work 节已通过静态校验并写入稿件文件",
                "checks": [{"kind": "no_error"}],
                "requires_gate": None,
                "on_failure": "fail",
            }
        )
    steps.append(
        {
            "title": "终编译",
            "action": "writing.compile",
            "params": {"phase": "final"},
            "acceptance": "全文编译 status=ok",
            "checks": [{"kind": "no_error"}],
            "requires_gate": None,
            "on_failure": "fail",
            # 终编译把已写的分节变成成稿 PDF：预算耗尽也放行（docs/voyage-loop.md §5.4）
            "wrapup": True,
        }
    )
    return steps


def presentation_plan(run: VoyageRun) -> list[dict[str, Any]]:
    """presentation 固定四步计划（论文分享 PPT）：
    取材 → 大纲（LLM）→ 甲板内容（LLM）→ 版式渲染 + 规范/视觉反馈迭代。
    固定管线不重规划：所有步骤 on_failure="fail"。
    """
    steps = [
        ("读取论文材料与配图", "present.collect", "论文正文/配图目录已汇总进 checkpoint"),
        ("设计分享大纲（LLM）", "present.outline", "已产出章节大纲 JSON"),
        ("生成幻灯片内容（LLM）", "present.slides", "甲板 JSON 已通过结构校验"),
        ("渲染 PPT + 反馈迭代", "present.build", "pptx 已生成并通过规范校验（视觉反馈尽力而为）"),
    ]
    return [
        {
            "title": title,
            "action": action,
            "params": {},
            "acceptance": acceptance,
            "checks": [{"kind": "no_error"}],
            "requires_gate": None,
            "on_failure": "fail",
        }
        for title, action, acceptance in steps
    ]


def paper_review_plan(run: VoyageRun) -> list[dict[str, Any]]:
    """paper_review 固定六步计划（docs/api-m5-c.md §1）：
    引用核验 → 事实查错 → 渲染稿件 → 评审员评审(×3) → 汇总(meta-review) → guardrail 校验。
    固定管线不重规划：所有步骤 on_failure="fail"。
    """
    steps = [
        ("引用核验", "review.citation_check", "全部 \\cite 已完成三态核验并写入 session payload"),
        ("事实查错", "review.fact_check", "数字比对 / claim 抽查 / \\ref 检查结果已落库"),
        ("渲染稿件", "review.render", "编译 PDF 前 9 页已渲染为 PNG（失败降级纯文本）"),
        ("评审员评审（×3）", "review.referees", "三位评审员意见已过 guardrail 并发布为消息"),
        ("汇总（meta-review）", "review.meta_review", "聚合评分与 meta 总结已写入 payload"),
        ("guardrail 校验", "review.guardrail", "通过判定与稿件流转已完成"),
    ]
    return [
        {
            "title": title,
            "action": action,
            "params": {},
            "acceptance": acceptance,
            "checks": [{"kind": "no_error"}],
            "requires_gate": None,
            "on_failure": "fail",
        }
        for title, action, acceptance in steps
    ]


def demo_plan(run: VoyageRun) -> list[dict[str, Any]]:
    """demo 航程固定三步：分析目标 → 生成产物（过闸门）→ 总结。"""
    return [
        {
            "title": "分析目标",
            "action": "llm.complete",
            "params": {
                "stage": "navigator",
                "prompt": "请分析以下科研目标，列出关键问题与切入点：{goal}",
            },
            "acceptance": "输出包含对目标的分析要点",
            "requires_gate": None,
        },
        {
            "title": "生成产物",
            "action": "artifact.write",
            "params": {
                "name": "demo-report.md",
                "content": "# Demo 任务产物\n\n目标：{goal}\n\n（由演示任务生成）\n",
            },
            "acceptance": "产物已写入 checkpoint",
            "requires_gate": "compute_budget",
        },
        {
            "title": "总结",
            "action": "llm.complete",
            "params": {
                "stage": "navigator",
                "prompt": "请对围绕以下目标的本次任务做一个简短总结：{goal}",
            },
            "acceptance": "输出包含总结内容",
            "requires_gate": None,
        },
    ]


def _extract_json(content: str) -> Any:
    """截取首个 '{' 到末个 '}' 之间的内容解析（容忍代码块围栏/前后杂讯）。"""
    start = content.find("{")
    end = content.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object found")
    return json.loads(content[start : end + 1])


def validate_steps(data: Any) -> list[dict[str, Any]]:
    """校验 {"steps": [...]} schema，返回规范化步骤列表；非法则抛 ValueError。"""
    if not isinstance(data, dict) or not isinstance(data.get("steps"), list):
        raise ValueError('expected {"steps": [...]}')
    steps: list[dict[str, Any]] = []
    actions = known_actions()
    for i, raw in enumerate(data["steps"]):
        if not isinstance(raw, dict):
            raise ValueError(f"step {i} is not an object")
        title = raw.get("title")
        action = raw.get("action")
        params = raw.get("params") or {}
        if not isinstance(title, str) or not title.strip():
            raise ValueError(f"step {i} missing title")
        if action not in actions:
            raise ValueError(f"step {i} has unknown action: {action!r}")
        if not isinstance(params, dict):
            raise ValueError(f"step {i} params is not an object")
        requires_gate = raw.get("requires_gate")
        if requires_gate is not None and not isinstance(requires_gate, str):
            raise ValueError(f"step {i} requires_gate must be string or null")
        acceptance = raw.get("acceptance")
        if acceptance is not None and not isinstance(acceptance, str):
            raise ValueError(f"step {i} acceptance must be string or null")
        checks = raw.get("checks")
        if checks is not None:
            try:
                checks = validate_checks(checks)
            except ValueError as e:
                raise ValueError(f"step {i} checks invalid: {e}") from e
        elif action not in _CONTENT_ACTIONS:
            # 平台批处理动作（wiki./forge./… 前缀）默认机械验收：无 error 即通过
            # （等价于旧 Sextant 前缀白名单，docs/voyage-loop.md §6）
            checks = [{"kind": "no_error"}]
        steps.append(
            {
                "title": title.strip(),
                "action": action,
                "params": params,
                "acceptance": acceptance,
                "checks": checks,
                "requires_gate": requires_gate or None,
            }
        )
    if not steps:
        raise ValueError("plan has no steps")
    return steps


def _workflow_templates_prompt(workflows: list[dict[str, Any]]) -> str:
    """自由规划 system prompt 的流程模板附录（项目启用的 workflow 技能）。"""
    lines = ["\n可用的流程模板（项目启用的流程技能）："]
    for w in workflows:
        titles = " → ".join(str(s.get("title", "")) for s in w.get("steps") or [])
        lines.append(f"- {w.get('slug')}：{w.get('name')}（步骤：{titles}）")
    lines.append(
        '若某个模板与目标匹配，直接输出 {"use_skill": "<slug>"}（不要再自拟 steps）；'
        "都不匹配才自行规划。"
    )
    return "\n".join(lines)


def _expand_workflow(slug: str, workflows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按 slug 展开 workflow 技能的步骤模板；slug 未知或 steps 非法抛 ValueError。"""
    entry = next((w for w in workflows if w.get("slug") == slug), None)
    if entry is None:
        raise ValueError(f"unknown workflow skill: {slug!r}")
    return validate_steps({"steps": entry.get("steps") or []})


class Navigator:
    def __init__(self, llm: LLMRouter) -> None:
        self._llm = llm

    async def _ask_for_steps(
        self,
        run: VoyageRun,
        system: str,
        user_prompt: str,
        workflows: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        last_error: Exception | None = None
        for _attempt in range(_MAX_ATTEMPTS):
            result = await self._llm.complete(
                "navigator",
                [
                    Message(role="system", content=system),
                    Message(role="user", content=user_prompt),
                ],
                user_id=run.created_by,
                project_id=run.project_id,
                voyage_id=run.id,
            )
            try:
                data = _extract_json(result.content)
                if workflows and isinstance(data, dict) and data.get("use_skill"):
                    return _expand_workflow(str(data["use_skill"]), workflows)
                return validate_steps(data)
            except (ValueError, json.JSONDecodeError) as e:
                last_error = e
        raise NavigatorError(f"navigator produced invalid plan: {last_error}")

    async def plan(self, run: VoyageRun, context: dict[str, Any] | None = None) -> list[dict]:
        """目标 → 步骤列表。demo / wiki_* kind 走固定计划模板，其余 kind 用 LLM 规划。"""
        if run.kind == "demo":
            return demo_plan(run)
        if run.kind in WIKI_KINDS:
            return wiki_plan(run)
        if run.kind == "idea_forge":
            return forge_plan(run)
        if run.kind == "idea_review":
            return review_plan(run)
        if run.kind == "idea_proposal":
            return proposal_plan(run)
        if run.kind == "experiment":
            return experiment_plan(run)
        if run.kind == "paper_writing":
            return writing_plan(run)
        if run.kind == "paper_review":
            return paper_review_plan(run)
        if run.kind == "presentation":
            return presentation_plan(run)
        system = PLAN_SYSTEM_PROMPT % {"actions": ", ".join(sorted(known_actions()))}
        workflows = skill_workflows(run.checkpoint or {})
        if workflows:
            system += _workflow_templates_prompt(workflows)
        user_prompt = f"目标：{run.goal}"
        if context:
            user_prompt += f"\n上下文：{json.dumps(context, ensure_ascii=False, default=str)}"
        return await self._ask_for_steps(run, system, user_prompt, workflows=workflows)

    async def replan(
        self, run: VoyageRun, failed_step: dict[str, Any], diagnosis: str
    ) -> list[dict[str, Any]]:
        """验证失败后重规划：返回替换「失败步骤起的剩余计划」的新步骤列表。

        idea_proposal 走确定性重规划（novelty 三档分支等），不经 LLM。
        """
        if run.kind == "idea_proposal":
            return proposal_replan(run, failed_step, diagnosis)
        system = REPLAN_SYSTEM_PROMPT % {"actions": ", ".join(sorted(known_actions()))}
        user_prompt = (
            f"目标：{run.goal}\n"
            f"原计划：{json.dumps(run.plan or [], ensure_ascii=False, default=str)}\n"
            f"失败步骤：{json.dumps(failed_step, ensure_ascii=False, default=str)}\n"
            f"失败诊断：{diagnosis}"
        )
        return await self._ask_for_steps(run, system, user_prompt)

    async def on_result(
        self,
        run: VoyageRun,
        failed_step: dict[str, Any],
        diagnosis: str,
        plan_state: str,
    ) -> dict[str, Any]:
        """loop 模式失败回灌：LLM 产出**计划编辑**而非替换尾部（docs/voyage-loop.md §5.3）。

        输出经 validate_plan_edit 严格校验（schema / 动作注册表 / 新增节点上限 /
        新节点必须带验收）；连续非法抛 NavigatorError（engine 转 paused_error）。
        """
        system = PLAN_EDIT_SYSTEM_PROMPT % {"actions": ", ".join(sorted(known_actions()))}
        user_prompt = (
            f"目标：{run.goal}\n"
            f"当前计划状态：\n{plan_state}\n"
            f"失败步骤：{json.dumps(failed_step, ensure_ascii=False, default=str)}\n"
            f"失败诊断：{diagnosis}"
        )
        last_error: Exception | None = None
        for _attempt in range(_MAX_ATTEMPTS):
            result = await self._llm.complete(
                "navigator",
                [
                    Message(role="system", content=system),
                    Message(role="user", content=user_prompt),
                ],
                user_id=run.created_by,
                project_id=run.project_id,
                voyage_id=run.id,
            )
            try:
                data = _extract_json(result.content)
                return validate_plan_edit(data, step_validator=validate_steps)
            except (ValueError, json.JSONDecodeError) as e:
                last_error = e
        raise NavigatorError(f"navigator produced invalid plan edit: {last_error}")
