"""AI 使用披露声明生成器（#691，设计报告 §18 信任设计③）。

期刊已普遍要求作者披露 AI 使用且禁止 AI 署名——与其让用户凭记忆写声明，不如把
平台已有的 AI 参与留痕**确定性**聚合成一份可直接粘贴的披露：零 LLM 调用、零新增
判断，报告说的每一句都能指回一条原始记录（与 discovery_disclosure 同一设计立场）。

数据来源（能取到什么就说什么，取不到的如实标注而不是编造）：

- ``llm_usage``：LLM 路由的持久记账（stage + model + tokens，按 voyage_id 归属）。
  这是**唯一**能给出真实模型名的持久来源——``llm_call_logs`` 默认关闭且仅留 7 天，
  不能作披露依据；checkpoint 的 node_usage 只有 token 没有模型名。
- ``voyage_runs``：run 清单与 kind。稿件与 run 之间没有外键，写作/审稿 run 把
  manuscript_id 记在 checkpoint.params 里，这里按同一约定反查（含已完结 run——
  services.manuscripts.find_active_writing_voyage 只查未完结的，不适用于披露）。
- ``manuscript_file_versions``：origin="pre_ai" 快照是「AI 写入前」的存档，快照数
  即 AI 写入次数的下界。人工编辑走 CRDT 实时协同、不逐次留版本，因此**无法逐条
  统计人工编辑**——这一局限直接写进 notes，宁可少说不编造。
"""

import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.llm_config import LLMUsage
from app.models.manuscript import Manuscript, ManuscriptFile, ManuscriptFileVersion
from app.models.voyage import VoyageRun

FACTS_VERSION = 1

# 与稿件关联的 run kind（都以 checkpoint.params.manuscript_id 回指稿件）
_MANUSCRIPT_RUN_KINDS = ("paper_writing", "paper_review")

# kind → 生成物类型（确定性映射；未登记的 kind 不猜，outputs 留空）
_OUTPUTS_BY_KIND: dict[str, tuple[str, ...]] = {
    "discovery": ("hypothesis_tree", "research_proposal"),
    "idea_forge": ("research_proposal",),
    "idea_review": ("research_proposal",),
    "idea_proposal": ("research_proposal",),
    "paper_writing": ("manuscript_text",),
    "paper_review": ("review_feedback",),
    "presentation": ("slides",),
    "wiki_bootstrap": ("literature_wiki",),
    "wiki_ingest": ("literature_wiki",),
    "daily_feed_sync": ("literature_feed",),
}


async def _runs_for_manuscript(
    session: AsyncSession, manuscript: Manuscript
) -> list[VoyageRun]:
    """稿件关联的全部写作/审稿 run（含已完结；checkpoint.params 回指判归属）。"""
    stmt = (
        select(VoyageRun)
        .where(
            VoyageRun.project_id == manuscript.project_id,
            VoyageRun.kind.in_(_MANUSCRIPT_RUN_KINDS),
        )
        .order_by(VoyageRun.created_at.asc())
    )
    wanted = str(manuscript.id)
    out: list[VoyageRun] = []
    for run in (await session.execute(stmt)).scalars().all():
        params = (run.checkpoint or {}).get("params") or {}
        if params.get("manuscript_id") == wanted:
            out.append(run)
    return out


async def _stage_rows_by_run(
    session: AsyncSession, run_ids: list[uuid.UUID]
) -> dict[uuid.UUID, list[dict[str, Any]]]:
    """llm_usage 按 (run, stage, model) 聚合；排序固定保证输出确定性。"""
    if not run_ids:
        return {}
    stmt = (
        select(
            LLMUsage.voyage_id,
            LLMUsage.stage,
            LLMUsage.model,
            func.count().label("calls"),
            func.coalesce(func.sum(LLMUsage.prompt_tokens), 0),
            func.coalesce(func.sum(LLMUsage.completion_tokens), 0),
        )
        .where(LLMUsage.voyage_id.in_(run_ids))
        .group_by(LLMUsage.voyage_id, LLMUsage.stage, LLMUsage.model)
        .order_by(LLMUsage.stage.asc(), LLMUsage.model.asc())
    )
    out: dict[uuid.UUID, list[dict[str, Any]]] = {}
    for voyage_id, stage, model, calls, prompt, completion in (
        await session.execute(stmt)
    ).all():
        out.setdefault(voyage_id, []).append(
            {
                "stage": stage,
                "model": model,
                "calls": int(calls),
                "prompt_tokens": int(prompt),
                "completion_tokens": int(completion),
            }
        )
    return out


def _run_facts(
    run: VoyageRun, stage_rows: list[dict[str, Any]], notes: list[str]
) -> dict[str, Any]:
    stages = list(stage_rows)
    if not stages:
        # 记账明细缺失但 run 累计用量表明确实有 LLM 参与（老 run / 明细被清）：
        # 模型名取不到就如实标 unknown，绝不猜一个模型名出来
        usage = run.usage or {}
        total = int(usage.get("prompt_tokens", 0)) + int(usage.get("completion_tokens", 0))
        if total > 0:
            stages = [
                {
                    "stage": "unknown",
                    "model": "unknown",
                    "calls": 0,
                    "prompt_tokens": int(usage.get("prompt_tokens", 0)),
                    "completion_tokens": int(usage.get("completion_tokens", 0)),
                }
            ]
            notes.append(f"model_unrecorded:{run.id}")
    return {
        "run_id": str(run.id),
        "kind": run.kind,
        "status": run.status,
        "goal": run.goal,
        "outputs": list(_OUTPUTS_BY_KIND.get(run.kind, ())),
        "stages": stages,
    }


async def _editing_facts(
    session: AsyncSession, manuscript_id: uuid.UUID, notes: list[str]
) -> dict[str, Any] | None:
    """稿件版本记录统计。没有任何版本时如实返回 None（notes 记 no_edit_history）。"""
    stmt = (
        select(
            ManuscriptFileVersion.origin,
            func.count(),
            func.count(func.distinct(ManuscriptFileVersion.file_id)),
        )
        .join(ManuscriptFile, ManuscriptFile.id == ManuscriptFileVersion.file_id)
        .where(ManuscriptFile.manuscript_id == manuscript_id)
        .group_by(ManuscriptFileVersion.origin)
    )
    rows = {
        origin: (int(n), int(files))
        for origin, n, files in (await session.execute(stmt)).all()
    }
    if not rows:
        notes.append("no_edit_history")
        return None
    ai_snapshots, ai_files = rows.get("pre_ai", (0, 0))
    # 人工编辑（CRDT 实时协同）不逐次留版本，只能给出 AI 写入快照数——如实说明
    notes.append("human_edits_not_itemized")
    return {
        "ai_write_snapshots": ai_snapshots,
        "files_with_ai_writes": ai_files,
        "compile_snapshots": rows.get("compile", (0, 0))[0],
        "restore_snapshots": rows.get("pre_restore", (0, 0))[0],
    }


async def build_disclosure_facts(
    session: AsyncSession,
    *,
    manuscript_id: uuid.UUID | None = None,
    run_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    """AI 参与事实的确定性聚合（权限判定在 API 层，这里只管拼事实）。

    manuscript_id / run_id 二选一。同一份数据两次调用输出一致（排序全部固定），
    供渲染层与前端直接消费。
    """
    if (manuscript_id is None) == (run_id is None):
        raise ValueError("必须且只能提供 manuscript_id 或 run_id 之一")

    notes: list[str] = []
    editing: dict[str, Any] | None = None

    if manuscript_id is not None:
        manuscript = await session.get(Manuscript, manuscript_id)
        if manuscript is None:
            raise ValueError("manuscript 不存在")
        subject = {
            "type": "manuscript",
            "id": str(manuscript.id),
            "title": manuscript.title,
        }
        runs = await _runs_for_manuscript(session, manuscript)
        if not runs:
            notes.append("no_runs_recorded")
        editing = await _editing_facts(session, manuscript.id, notes)
    else:
        run = await session.get(VoyageRun, run_id)
        if run is None:
            raise ValueError("voyage run 不存在")
        subject = {
            "type": "voyage",
            "id": str(run.id),
            "title": run.goal or run.kind,
        }
        runs = [run]

    rows_by_run = await _stage_rows_by_run(session, [r.id for r in runs])
    run_facts = [_run_facts(r, rows_by_run.get(r.id, []), notes) for r in runs]

    models: set[str] = set()
    stages: set[str] = set()
    totals = {"prompt_tokens": 0, "completion_tokens": 0, "calls": 0}
    for rf in run_facts:
        for row in rf["stages"]:
            if row["model"] != "unknown":
                models.add(row["model"])
            if row["stage"] != "unknown":
                stages.add(row["stage"])
            totals["prompt_tokens"] += row["prompt_tokens"]
            totals["completion_tokens"] += row["completion_tokens"]
            totals["calls"] += row["calls"]

    ai_used = any(rf["stages"] for rf in run_facts) or bool(
        editing and editing["ai_write_snapshots"] > 0
    )

    return {
        "version": FACTS_VERSION,
        "subject": subject,
        "ai_used": ai_used,
        "runs": run_facts,
        "models": sorted(models),
        "stages": sorted(stages),
        "totals": totals,
        "editing": editing,
        "notes": notes,
    }


# ---- 声明渲染（纯函数，模板硬编码） ----

STYLES = ("icmje", "elsevier", "generic")
LANGS = ("zh", "en")

# 环节 → 用途措辞（未登记的环节直接用环节名，不猜用途）
_STAGE_PURPOSES: dict[str, dict[str, str]] = {
    "writing": {"zh": "起草稿件章节文本", "en": "drafting manuscript section text"},
    "review": {"zh": "生成审稿意见", "en": "generating review feedback"},
    "discovery_plan": {"zh": "规划文献探索", "en": "planning the literature exploration"},
    "hyp_generate": {"zh": "生成研究假设", "en": "generating research hypotheses"},
    "hyp_ground": {"zh": "为假设检索文献依据", "en": "grounding hypotheses in the literature"},
    "hyp_novelty": {"zh": "评估假设新颖性", "en": "assessing hypothesis novelty"},
    "hyp_feasibility": {"zh": "评估假设可行性", "en": "assessing hypothesis feasibility"},
    "hyp_compare": {"zh": "假设两两对比评审", "en": "pairwise comparison of hypotheses"},
    "proposal": {"zh": "撰写研究方案", "en": "drafting the research proposal"},
    "librarian": {
        "zh": "组织演示与资料内容",
        "en": "organizing presentation and reference content",
    },
    "embedding": {"zh": "文献向量化检索", "en": "embedding-based literature retrieval"},
    "rerank": {"zh": "检索结果重排", "en": "reranking retrieved results"},
}

_OUTPUT_LABELS: dict[str, dict[str, str]] = {
    "hypothesis_tree": {"zh": "假设树", "en": "hypothesis tree"},
    "research_proposal": {"zh": "研究方案", "en": "research proposal"},
    "manuscript_text": {"zh": "稿件文本", "en": "manuscript text"},
    "review_feedback": {"zh": "审稿意见", "en": "review feedback"},
    "slides": {"zh": "演示图文", "en": "presentation slides"},
    "literature_wiki": {"zh": "文献综述 wiki", "en": "literature wiki"},
    "literature_feed": {"zh": "文献动态", "en": "literature feed"},
}

# notes 代码 → 如实说明（进附录，让读者知道口径边界）
_NOTE_LABELS: dict[str, dict[str, str]] = {
    "no_runs_recorded": {
        "zh": "平台未记录到与本稿件关联的 AI 任务。",
        "en": "No AI runs associated with this manuscript were recorded on the platform.",
    },
    "no_edit_history": {
        "zh": "本稿件没有版本记录，无法统计编辑痕迹。",
        "en": "This manuscript has no version history; editing traces cannot be reported.",
    },
    "human_edits_not_itemized": {
        "zh": "人工编辑经实时协同进行、不逐次留版本，故仅统计 AI 写入快照数，未逐条统计人工编辑。",
        "en": (
            "Human edits happen via real-time collaboration without per-edit versioning; "
            "only AI-write snapshots are counted, individual human edits are not itemized."
        ),
    },
    "model_unrecorded": {
        "zh": "部分任务的用量明细缺失，模型名如实标注为 unknown。",
        "en": (
            "Usage details are missing for some runs; "
            "the model name is honestly reported as unknown."
        ),
    },
}

# 三家风格模板。措辞依据：
# - icmje：ICMJE Recommendations（作者应在投稿时披露是否使用 AI 辅助技术及其用途；
#   作者对内容负全责；AI 不得署名，因其无法对工作负责）。
# - elsevier：Elsevier 生成式 AI 作者政策（声明段落置于稿末参考文献前的独立小节，
#   固定句式 "During the preparation of this work the author(s) used X in order to Y.
#   After using this tool/service, the author(s) reviewed and edited …"）。
# - generic：中性措辞，适配未指定风格的场合。
# {tools} = 模型清单；{purposes} = 用途清单。
_TEMPLATES: dict[tuple[str, str], dict[str, str]] = {
    ("icmje", "en"): {
        "used": (
            "In accordance with the ICMJE recommendations, the authors disclose the use of "
            "artificial intelligence (AI)-assisted technologies in the preparation of this work. "
            "The following tools were used: {tools}. They were used for: {purposes}. "
            "The authors reviewed and edited the AI-assisted material and take full "
            "responsibility for the integrity and accuracy of the content of this work. "
            "No AI tool is listed as an author, as AI cannot be accountable for the work."
        ),
        "unused": (
            "In accordance with the ICMJE recommendations, the authors declare that, based on "
            "the platform's recorded activity, no artificial intelligence (AI)-assisted "
            "technologies were used in the preparation of this work."
        ),
    },
    ("icmje", "zh"): {
        "used": (
            "按照 ICMJE 建议，作者披露本研究工作在准备过程中使用了人工智能（AI）辅助技术。"
            "所用工具：{tools}。用途：{purposes}。"
            "作者已审阅并编辑 AI 辅助生成的内容，对本文内容的完整性与准确性负全部责任；"
            "未将任何 AI 工具列为作者——AI 无法对研究工作负责。"
        ),
        "unused": (
            "按照 ICMJE 建议，作者声明：根据平台记录，本研究工作的准备过程中"
            "未使用人工智能（AI）辅助技术。"
        ),
    },
    ("elsevier", "en"): {
        "used": (
            "Declaration of generative AI and AI-assisted technologies in the writing process.\n\n"
            "During the preparation of this work the author(s) used {tools} in order to "
            "{purposes}. After using these tools/services, the author(s) reviewed and edited "
            "the content as needed and take(s) full responsibility for the content of the "
            "published article.\n\n"
            "(Per Elsevier policy, place this statement in a dedicated section at the end of "
            "the manuscript, before the references.)"
        ),
        "unused": (
            "Declaration of generative AI and AI-assisted technologies in the writing process.\n\n"
            "Based on the platform's recorded activity, the author(s) did not use generative AI "
            "or AI-assisted technologies during the preparation of this work."
        ),
    },
    ("elsevier", "zh"): {
        "used": (
            "关于写作过程中使用生成式 AI 与 AI 辅助技术的声明。\n\n"
            "在本研究工作的准备过程中，作者使用了 {tools}，用于{purposes}。"
            "使用上述工具/服务后，作者已按需审阅并编辑相关内容，"
            "并对发表文章的内容负全部责任。\n\n"
            "（按 Elsevier 政策，本段应置于稿件末尾、参考文献之前的独立小节。）"
        ),
        "unused": (
            "关于写作过程中使用生成式 AI 与 AI 辅助技术的声明。\n\n"
            "根据平台记录，作者在本研究工作的准备过程中未使用生成式 AI 或 AI 辅助技术。"
        ),
    },
    ("generic", "en"): {
        "used": (
            "AI Use Disclosure. Parts of this work were prepared with the assistance of AI "
            "tools: {tools}. These tools were used for: {purposes}. All AI-assisted output "
            "was reviewed by the authors, who take full responsibility for the final content."
        ),
        "unused": (
            "AI Use Disclosure. Based on the platform's recorded activity, no AI tools were "
            "used in the preparation of this work."
        ),
    },
    ("generic", "zh"): {
        "used": (
            "AI 使用披露。本研究工作的部分内容在 AI 工具辅助下完成，所用工具：{tools}。"
            "用途：{purposes}。所有 AI 辅助产出均经作者审阅，作者对最终内容负全部责任。"
        ),
        "unused": "AI 使用披露。根据平台记录，本研究工作的准备过程中未使用 AI 工具。",
    },
}


def _join(items: list[str], lang: str) -> str:
    return "、".join(items) if lang == "zh" else ", ".join(items)


def _tools_clause(facts: dict[str, Any], lang: str) -> str:
    if facts["models"]:
        return _join(facts["models"], lang)
    # 有 AI 参与但模型名没记到账上：如实说「未记录到模型名」而不是编一个
    if lang == "zh":
        return "未记录到具体模型名的 AI 模型"
    return "an AI model whose name was not recorded"


def _purposes_clause(facts: dict[str, Any], lang: str) -> str:
    purposes: list[str] = []
    for stage in facts["stages"]:
        label = _STAGE_PURPOSES.get(stage, {}).get(lang)
        purposes.append(label if label else stage)
    if not purposes:
        return "未记录到具体用途" if lang == "zh" else "purposes that were not recorded in detail"
    return _join(purposes, lang)


def _note_label(code: str, lang: str) -> str:
    # model_unrecorded 带 run_id 后缀，按前缀查文案
    key = code.split(":", 1)[0]
    label = _NOTE_LABELS.get(key, {}).get(lang)
    return label if label else code


def _appendix(facts: dict[str, Any], lang: str) -> str:
    zh = lang == "zh"
    lines: list[str] = []
    lines.append("## AI 参与明细" if zh else "## AI participation details")
    lines.append("")
    if facts["runs"]:
        header = (
            "| 任务 | 类型 | 生成物 | 环节 | 模型 | 调用数 | 输入 tokens | 输出 tokens |"
            if zh
            else (
                "| Run | Kind | Outputs | Stage | Model "
                "| Calls | Prompt tokens | Completion tokens |"
            )
        )
        lines.append(header)
        lines.append("|---|---|---|---|---|---|---|---|")
        for rf in facts["runs"]:
            outputs = (
                _join([_OUTPUT_LABELS.get(o, {}).get(lang, o) for o in rf["outputs"]], lang)
                or "-"
            )
            if rf["stages"]:
                for row in rf["stages"]:
                    lines.append(
                        f"| {rf['run_id']} | {rf['kind']} | {outputs} | {row['stage']} "
                        f"| {row['model']} | {row['calls'] or '-'} "
                        f"| {row['prompt_tokens']} | {row['completion_tokens']} |"
                    )
            else:
                lines.append(
                    f"| {rf['run_id']} | {rf['kind']} | {outputs} | - | - | - | - | - |"
                )
        totals = facts["totals"]
        lines.append("")
        lines.append(
            f"合计：{totals['calls']} 次调用，输入 {totals['prompt_tokens']} tokens，"
            f"输出 {totals['completion_tokens']} tokens。"
            if zh
            else f"Total: {totals['calls']} calls, {totals['prompt_tokens']} prompt tokens, "
            f"{totals['completion_tokens']} completion tokens."
        )
    else:
        lines.append(
            "（未记录到关联的 AI 任务。）" if zh else "(No associated AI runs were recorded.)"
        )

    editing = facts.get("editing")
    if editing is not None:
        lines.append("")
        lines.append("### 编辑痕迹" if zh else "### Editing traces")
        lines.append("")
        if zh:
            lines.append(
                f"- AI 写入快照：{editing['ai_write_snapshots']} 次"
                f"（涉及 {editing['files_with_ai_writes']} 个文件）"
            )
            lines.append(f"- 编译快照：{editing['compile_snapshots']} 次")
            lines.append(f"- 版本回退快照：{editing['restore_snapshots']} 次")
        else:
            lines.append(
                f"- AI-write snapshots: {editing['ai_write_snapshots']} "
                f"(across {editing['files_with_ai_writes']} files)"
            )
            lines.append(f"- Compile snapshots: {editing['compile_snapshots']}")
            lines.append(f"- Restore snapshots: {editing['restore_snapshots']}")

    if facts["notes"]:
        lines.append("")
        lines.append("### 记录口径说明" if zh else "### Notes on data coverage")
        lines.append("")
        seen: set[str] = set()
        for code in facts["notes"]:
            label = _note_label(code, lang)
            if label not in seen:
                seen.add(label)
                lines.append(f"- {label}")
    return "\n".join(lines)


def render_disclosure(facts: dict[str, Any], style: str, lang: str) -> dict[str, str]:
    """facts → {statement, appendix}（纯文本段落 + Markdown 附录，纯函数）。"""
    if style not in STYLES:
        raise ValueError(f"unknown style: {style}")
    if lang not in LANGS:
        raise ValueError(f"unknown lang: {lang}")
    template = _TEMPLATES[(style, lang)]
    if facts["ai_used"]:
        statement = template["used"].format(
            tools=_tools_clause(facts, lang), purposes=_purposes_clause(facts, lang)
        )
    else:
        statement = template["unused"]
    return {"statement": statement, "appendix": _appendix(facts, lang)}
