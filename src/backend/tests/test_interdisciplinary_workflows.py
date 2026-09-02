"""Interdisciplinary profile and Skill versions stay pinned across project workflows."""

import uuid

from sqlalchemy import select

from app.agents.voyage.actions import ActionContext
from app.agents.voyage.actions_experiment import _prompt_with_context
from app.agents.voyage.engine import VoyageEngine
from app.core.db import get_sessionmaker
from app.core.llm.router import LLMRouter
from app.models.project import Project
from app.models.skill import Skill, SkillVersion
from app.models.voyage import VoyageRun
from app.services.interdisciplinary_workflows import SKILL_SLUG
from app.services.skills import ensure_builtin_skills
from tests.conftest import RecordingBus, register_and_login


def _scope(version: int = 1) -> dict:
    return {
        "research_scope": (
            f"Couple structural mechanics and visual measurement, revision {version}."
        ),
        "core_questions": ["Which visual observables preserve mechanical meaning?"],
        "primary_domain": "Structural engineering",
        "related_domains": ["Computer vision", "Data science"],
        "evidence_boundary": "Use only validated structural and visual evidence.",
        "validation_conditions": ["Compare against independent impact experiments."],
        "user_questions": [],
    }


async def _project_and_profile(client) -> tuple[dict[str, str], str]:
    token = await register_and_login(client, email="workflow-owner@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    project = await client.post(
        "/api/projects",
        headers=headers,
        json={
            "name": "Mechanics and vision",
            "statement": "Measure impact response with segmented video.",
            "research_mode": "interdisciplinary",
        },
    )
    project_id = project.json()["id"]
    saved = await client.put(
        f"/api/projects/{project_id}/interdisciplinary/scope",
        headers=headers,
        json=_scope(),
    )
    assert saved.status_code == 200, saved.text
    confirmed = await client.post(
        f"/api/projects/{project_id}/interdisciplinary/scope/confirm", headers=headers
    )
    assert confirmed.status_code == 200, confirmed.text
    return headers, project_id


async def _snapshot_run(project_id: str) -> VoyageRun:
    async with get_sessionmaker()() as session:
        await ensure_builtin_skills(session)
        project = await session.get(Project, uuid.UUID(project_id))
        run = VoyageRun(
            kind="idea_forge",
            goal="Generate interdisciplinary ideas",
            status="planning",
            project_id=project.id,
            created_by=project.owner_id,
        )
        session.add(run)
        await session.commit()
        await session.refresh(run)
        engine = VoyageEngine(event_bus=RecordingBus(), llm_router=LLMRouter())
        await engine._ensure_skills_snapshot(session, run)
        await session.refresh(run)
        return run


async def test_run_pins_confirmed_profile_and_builtin_skill_versions(client):
    headers, project_id = await _project_and_profile(client)
    first = await _snapshot_run(project_id)
    first_context = dict((first.checkpoint or {})["interdisciplinary_context"])
    assert first_context["profile_version"] == 1
    assert first_context["skill_version"] == 1
    assert first_context["primary_domain"] == "Structural engineering"
    assert first_context["related_domains"] == ["Computer vision", "Data science"]

    skills = (first.checkpoint or {})["skills"]
    assert skills["navigator.free_plan"][0]["kind"] == "workflow"
    assert skills["forge.generate"][0]["kind"] == "guidance"
    assert "Core bridge questions" in skills["forge.generate"][0]["body"]
    assert "Discipline query channels and terms" in skills["forge.generate"][0]["body"]
    assert "Structural engineering" in skills["forge.generate"][0]["body"]
    assert "Computer vision" in skills["forge.generate"][0]["body"]
    assert first_context["profile_version_id"] in skills["forge.generate"][0]["body"]

    ctx = ActionContext(run=first, llm=LLMRouter(), checkpoint=dict(first.checkpoint or {}))
    assert "Structural engineering" in _prompt_with_context("BASE", ctx)

    async with get_sessionmaker()() as session:
        skill = await session.scalar(select(Skill).where(Skill.slug == SKILL_SLUG))
        first_skill_version = await session.scalar(
            select(SkillVersion).where(
                SkillVersion.skill_id == skill.id,
                SkillVersion.version == first_context["skill_version"],
            )
        )
        session.add(
            SkillVersion(
                skill_id=skill.id,
                version=first_skill_version.version + 1,
                manifest=dict(first_skill_version.manifest),
                body=f"{first_skill_version.body}\n\nVersion two guidance.",
                changelog="Test workflow version pinning.",
            )
        )
        await session.commit()

    updated = await client.put(
        f"/api/projects/{project_id}/interdisciplinary/scope",
        headers=headers,
        json=_scope(2),
    )
    assert updated.status_code == 200, updated.text
    confirmed = await client.post(
        f"/api/projects/{project_id}/interdisciplinary/scope/confirm", headers=headers
    )
    assert confirmed.status_code == 200, confirmed.text

    second = await _snapshot_run(project_id)
    second_context = (second.checkpoint or {})["interdisciplinary_context"]
    assert second_context["profile_version"] == 2
    assert second_context["profile_version_id"] != first_context["profile_version_id"]
    assert second_context["skill_version"] == first_context["skill_version"] + 1
    assert (first.checkpoint or {})["interdisciplinary_context"] == first_context


async def test_conventional_project_keeps_existing_skill_behavior(client):
    token = await register_and_login(client, email="conventional-workflow@example.com")
    project = await client.post(
        "/api/projects",
        headers={"Authorization": f"Bearer {token}"},
        json={"name": "Conventional mechanics", "research_mode": "conventional"},
    )
    run = await _snapshot_run(project.json()["id"])
    assert "interdisciplinary_context" not in (run.checkpoint or {})
    assert all(
        entry.get("slug") != "interdisciplinary-research-workflow"
        for entries in (run.checkpoint or {})["skills"].values()
        for entry in entries
    )


async def test_placeholder_disciplines_cannot_be_confirmed(client):
    token = await register_and_login(client, email="placeholder-workflow@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    project = await client.post(
        "/api/projects",
        headers=headers,
        json={"name": "Invalid scope", "research_mode": "interdisciplinary"},
    )
    project_id = project.json()["id"]
    saved = await client.put(
        f"/api/projects/{project_id}/interdisciplinary/scope",
        headers=headers,
        json={**_scope(), "primary_domain": "Pending user confirmation"},
    )
    assert saved.status_code == 200, saved.text
    confirmed = await client.post(
        f"/api/projects/{project_id}/interdisciplinary/scope/confirm", headers=headers
    )
    assert confirmed.status_code == 422
    assert confirmed.json()["detail"] == "INTERDISCIPLINARY_SCOPE_INVALID"


async def test_a_running_voyage_keeps_its_pinned_context_when_the_profile_moves(client):
    """已经冻结过的 run，profile 再确认新版本也不能改它的快照。

    这是 PR 描述里「resume 和 replay 读同一份不可变快照」那句的实际含义：
    中途换了 profile 还去改进行中任务的上下文，等于同一次 run 前后按两套口径产出，
    而回放时看到的又是最后那一套——出了问题根本对不上账。
    """
    headers, project_id = await _project_and_profile(client)
    run = await _snapshot_run(project_id)
    pinned = dict((run.checkpoint or {})["interdisciplinary_context"])
    assert pinned["profile_version"] == 1

    await client.put(
        f"/api/projects/{project_id}/interdisciplinary/scope", headers=headers, json=_scope(2)
    )
    await client.post(
        f"/api/projects/{project_id}/interdisciplinary/scope/confirm", headers=headers
    )

    # 对同一个 run 再驱动一次（等价于断点恢复）
    async with get_sessionmaker()() as session:
        same = await session.get(VoyageRun, run.id)
        engine = VoyageEngine(event_bus=RecordingBus(), llm_router=LLMRouter())
        await engine._ensure_skills_snapshot(session, same)
        await session.refresh(same)
        after = dict((same.checkpoint or {})["interdisciplinary_context"])

    assert after["profile_version"] == 1, "进行中的 run 被改成了新版本"
    assert after["profile_version_id"] == pinned["profile_version_id"]
