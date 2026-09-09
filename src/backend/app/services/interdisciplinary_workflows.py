"""Freeze confirmed interdisciplinary constraints into project workflow runs."""

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.guidance_document import GuidanceDocument
from app.models.interdisciplinary import InterdisciplinaryResearchProfileVersion
from app.models.project import Project

SKILL_SLUG = "interdisciplinary-research-workflow"

_GUIDANCE_TARGETS = (
    "wiki.score_relevance",
    "wiki.compile",
    "wiki.daily_digest",
    "wiki.trend_synthesize",
    "forge.gap_analysis",
    "forge.generate",
    "forge.score",
    "experiment.plan",
    "writing.section",
    "writing.related_work",
    "review.referees",
    "present.outline",
    "present.slides",
)
_PLACEHOLDERS = (
    "pending",
    "unknown",
    "to be confirmed",
    "tbd",
    "待确认",
    "待用户确认",
    "未确认",
    "未确定",
    "未知",
)

# ---- 内置指引文档种子（#741：从 v1 BUILTIN_SKILLS 原文迁入，正文逐字节不动）----
# 原先这份内容作为 v1 builtin 技能种子活在 skills/skill_versions 里——v1 已冻结，
# 这里是唯一还在写它的新功能。现在它落在 guidance_documents（见模型 docstring
# 里对「为什么不进 v2」的论证），注入行为不变。

GUIDANCE_SEED: dict[str, Any] = {
    "slug": SKILL_SLUG,
    "name": "跨学科研究工作流",
    "targets": [
        "navigator.free_plan",
        "wiki.score_relevance",
        "forge.generate",
        "experiment.plan",
        "writing.section",
        "writing.related_work",
        "review.referees",
        "present.outline",
    ],
    "steps": [
        {
            "title": "确认交叉研究范围",
            "action": "llm.complete",
            "params": {
                "stage": "planning",
                "prompt": (
                    "读取课题已确认的主学科、关联学科、核心问题和证据边界。"
                    "如果任一字段缺失，先提出最少的澄清问题，不要自行补全学科。"
                ),
            },
            "acceptance": "输出主学科、关联学科、核心问题和证据边界，并标明待确认项",
            "requires_gate": None,
        },
        {
            "title": "分学科检索并合并证据",
            "action": "llm.complete",
            "params": {
                "stage": "librarian",
                "prompt": (
                    "先按主学科与各关联学科分别检索，再用统一 DOI/标题身份合并去重。"
                    "每篇文献保留来源学科、贡献类型、相关性理由和句子级证据，"
                    "不能把共享关键词当作跨学科关联。"
                ),
            },
            "acceptance": "输出分学科证据表，并有跨学科合并后的去重文献清单",
            "requires_gate": None,
        },
        {
            "title": "生成可证伪研究想法",
            "action": "llm.complete",
            "params": {
                "stage": "ideation",
                "prompt": (
                    "基于已确认范围和分学科证据，生成有明确机制的交叉想法。"
                    "每个想法必须说明主学科承担的核心问题、关联学科提供的不可替代方法、"
                    "最小可行实验、风险和可证伪判据。"
                ),
            },
            "acceptance": "每个想法都有机制差异、最小实验和证据引用",
            "requires_gate": None,
        },
        {
            "title": "贯穿实验、写作和评审",
            "action": "llm.complete",
            "params": {
                "stage": "research",
                "prompt": (
                    "后续实验计划、论文写作、PPT提纲和评审均携带同一交叉范围版本。"
                    "所有引用采用文献编号+句子编号，找不到句子时退回段落或论文级，"
                    "不得把某一学科的指标冒充另一学科的结论。"
                ),
            },
            "acceptance": "产物标注交叉研究上下文版本并保留可回溯引用",
            "requires_gate": None,
        },
    ],
    "body": (
        "跨学科研究必须先确认范围资产，再进入后续流程。\n\n"
        "- 主学科负责定义核心科学问题；关联学科必须提供不可替代的方法、数据或评价标准。\n"
        "- 检索阶段按学科分桶以避免中文/英文术语和领域评价标准互相污染；合并阶段按 DOI、"
        "外部标识和规范化标题去重。\n"
        "- 任何 AI 结论都要携带交叉范围版本和证据锚点；句子级找不到时按既定规则回退，"
        "不能静默改成无来源的摘要推断。\n"
        "- 常规课题不注入本技能，不改变原有的单学科检索和写作流程。"
    ),
}


async def ensure_guidance_documents(session: AsyncSession) -> int:
    """按 slug 幂等插入内置指引文档 v1，返回新插入数量。已有任意版本则不动。

    只在 slug 完全缺席时种入：存量部署由迁移把 v1 技能行搬过来（可能已有更高
    版本），种子不能把它盖回 v1。
    """
    exists = await session.scalar(
        select(GuidanceDocument.id).where(GuidanceDocument.slug == GUIDANCE_SEED["slug"]).limit(1)
    )
    if exists is not None:
        return 0
    session.add(
        GuidanceDocument(
            slug=GUIDANCE_SEED["slug"],
            version=1,
            name=GUIDANCE_SEED["name"],
            body=GUIDANCE_SEED["body"],
            targets=list(GUIDANCE_SEED["targets"]),
            steps=[dict(step) for step in GUIDANCE_SEED["steps"]],
        )
    )
    await session.commit()
    return 1


class InterdisciplinaryScopeInvalidError(ValueError):
    pass


def validate_disciplines(primary_domain: str, related_domains: list[str]) -> None:
    """Reject placeholder disciplines before they become confirmed workflow assets."""

    values = [primary_domain, *related_domains]
    normalized = [str(value or "").strip().casefold() for value in values]
    if not normalized[0] or not related_domains or any(not value for value in normalized):
        raise InterdisciplinaryScopeInvalidError("INTERDISCIPLINARY_SCOPE_INVALID")
    if any(marker in value for value in normalized for marker in _PLACEHOLDERS):
        raise InterdisciplinaryScopeInvalidError("INTERDISCIPLINARY_SCOPE_INVALID")


async def _latest_confirmed_profile(
    session: AsyncSession, project_id: uuid.UUID
) -> InterdisciplinaryResearchProfileVersion | None:
    return await session.scalar(
        select(InterdisciplinaryResearchProfileVersion)
        .where(
            InterdisciplinaryResearchProfileVersion.project_id == project_id,
            InterdisciplinaryResearchProfileVersion.status == "confirmed",
        )
        .order_by(InterdisciplinaryResearchProfileVersion.version.desc())
        .limit(1)
    )


async def _latest_guidance_document(session: AsyncSession) -> GuidanceDocument | None:
    return await session.scalar(
        select(GuidanceDocument)
        .where(GuidanceDocument.slug == SKILL_SLUG)
        .order_by(GuidanceDocument.version.desc())
        .limit(1)
    )


async def snapshot_for_project(
    session: AsyncSession, project_id: uuid.UUID | None
) -> dict[str, Any] | None:
    """Return the latest confirmed profile and guidance document as one immutable payload."""

    if project_id is None:
        return None
    project = await session.get(Project, project_id)
    if project is None or project.research_mode != "interdisciplinary":
        return None
    profile = await _latest_confirmed_profile(session, project_id)
    if profile is None:
        return None
    validate_disciplines(profile.primary_domain, list(profile.related_domains or []))
    document = await _latest_guidance_document(session)
    if document is None:
        raise RuntimeError("INTERDISCIPLINARY_WORKFLOW_SKILL_MISSING")
    return {
        "schema_version": 1,
        "project_id": str(project_id),
        "profile_id": str(profile.profile_id),
        "profile_version_id": str(profile.id),
        "profile_version": profile.version,
        # 指引文档搬离 v1 后没有「技能/版本」两级——每个版本就是一行。两个键都指
        # 同一行，键名保留是为了 checkpoint schema_version=1 不破（存量 run 回放）。
        "skill_id": str(document.id),
        "skill_version_id": str(document.id),
        "skill_version": document.version,
        "skill_slug": document.slug,
        "skill_name": document.name,
        "skill_body": document.body,
        "skill_manifest": {
            "targets": list(document.targets or []),
            "steps": [dict(step) for step in document.steps or []],
        },
        "research_scope": profile.research_scope,
        "core_questions": list(profile.core_questions or []),
        "primary_domain": profile.primary_domain,
        "related_domains": list(profile.related_domains or []),
        "evidence_boundary": profile.evidence_boundary,
        "validation_conditions": list(profile.validation_conditions or []),
        "query_matrix": list(profile.query_matrix or []),
        "evidence_balance": dict(profile.evidence_balance or {}),
    }


def _render_guidance(context: dict[str, Any]) -> str:
    related = ", ".join(context["related_domains"])
    questions = "\n".join(f"- {item}" for item in context["core_questions"])
    conditions = "\n".join(f"- {item}" for item in context["validation_conditions"])
    balance = ", ".join(
        f"{domain}: {weight}" for domain, weight in context["evidence_balance"].items()
    )
    channels = "\n".join(
        f"- [{row.get('role') or 'unspecified'}] {row.get('discipline') or 'unspecified'}: "
        f"{str(row.get('query') or '').strip()[:500]}"
        for row in context["query_matrix"][:24]
        if isinstance(row, dict) and str(row.get("query") or "").strip()
    )
    return (
        "Immutable interdisciplinary research context for this run:\n"
        f"- Profile version: {context['profile_version']} "
        f"({context['profile_version_id']})\n"
        f"- Primary discipline: {context['primary_domain']}\n"
        f"- Associated disciplines: {related}\n"
        f"- Research scope: {context['research_scope']}\n"
        f"- Core bridge questions:\n{questions}\n"
        f"- Evidence boundary: {context['evidence_boundary'] or 'Not specified'}\n"
        f"- Validation conditions:\n{conditions or '- Not specified'}\n"
        f"- Evidence balance: {balance or 'Not specified'}\n\n"
        f"- Discipline query channels and terms:\n{channels or '- Not specified'}\n\n"
        "Apply these constraints throughout the output. Keep the primary discipline responsible "
        "for the scientific question, identify the non-substitutable contribution of each "
        "associated discipline, preserve discipline-specific evidence standards, and state "
        "whether every bridge claim has balanced supporting evidence.\n\n"
        f"Pinned workflow Skill v{context['skill_version']}:\n{context['skill_body']}"
    )


def apply_to_skill_snapshot(
    snapshot: dict[str, list[dict[str, Any]]], context: dict[str, Any]
) -> None:
    """Add one pinned workflow entry and guidance entries without duplicating user configuration."""

    common = {
        "skill_id": context["skill_id"],
        "skill_version_id": context["skill_version_id"],
        "slug": context["skill_slug"],
        "name": context["skill_name"],
        "version": context["skill_version"],
        "config": {"profile_version_id": context["profile_version_id"]},
        "personas": [],
        "output_contract": None,
    }
    workflow_entry = {
        **common,
        "kind": "workflow",
        "body": context["skill_body"],
        "steps": context["skill_manifest"].get("steps") or [],
    }
    navigator_entries = snapshot.setdefault("navigator.free_plan", [])
    if not any(entry.get("slug") == SKILL_SLUG for entry in navigator_entries):
        navigator_entries.append(workflow_entry)

    guidance = _render_guidance(context)
    for target in _GUIDANCE_TARGETS:
        entries = snapshot.setdefault(target, [])
        if any(entry.get("slug") == SKILL_SLUG for entry in entries):
            continue
        entries.append(
            {
                **common,
                "kind": "guidance",
                "body": guidance,
                "steps": [],
            }
        )
