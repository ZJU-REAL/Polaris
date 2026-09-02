"""Freeze confirmed interdisciplinary constraints into project workflow runs."""

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.interdisciplinary import InterdisciplinaryResearchProfileVersion
from app.models.project import Project
from app.models.skill import Skill, SkillVersion

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


async def _builtin_skill_version(session: AsyncSession) -> tuple[Skill, SkillVersion] | None:
    skill = await session.scalar(
        select(Skill).where(
            Skill.slug == SKILL_SLUG,
            Skill.scope == "builtin",
            Skill.is_archived.is_(False),
        )
    )
    if skill is None:
        return None
    version = await session.scalar(
        select(SkillVersion)
        .where(SkillVersion.skill_id == skill.id)
        .order_by(SkillVersion.version.desc())
        .limit(1)
    )
    return (skill, version) if version is not None else None


async def snapshot_for_project(
    session: AsyncSession, project_id: uuid.UUID | None
) -> dict[str, Any] | None:
    """Return the latest confirmed profile and current built-in Skill as one immutable payload."""

    if project_id is None:
        return None
    project = await session.get(Project, project_id)
    if project is None or project.research_mode != "interdisciplinary":
        return None
    profile = await _latest_confirmed_profile(session, project_id)
    if profile is None:
        return None
    validate_disciplines(profile.primary_domain, list(profile.related_domains or []))
    skill_pair = await _builtin_skill_version(session)
    if skill_pair is None:
        raise RuntimeError("INTERDISCIPLINARY_WORKFLOW_SKILL_MISSING")
    skill, version = skill_pair
    return {
        "schema_version": 1,
        "project_id": str(project_id),
        "profile_id": str(profile.profile_id),
        "profile_version_id": str(profile.id),
        "profile_version": profile.version,
        "skill_id": str(skill.id),
        "skill_version_id": str(version.id),
        "skill_version": version.version,
        "skill_slug": skill.slug,
        "skill_name": skill.name,
        "skill_body": version.body,
        "skill_manifest": dict(version.manifest or {}),
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
