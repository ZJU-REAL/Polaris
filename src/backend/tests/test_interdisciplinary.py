"""Smoke tests for the interdisciplinary project/profile/library contract."""

import pytest

from tests.conftest import register_and_login


@pytest.mark.asyncio
async def test_scope_confirmation_creates_one_dedicated_library(client):
    token = await register_and_login(client, email="interdisciplinary-owner@example.com")
    auth = {"Authorization": f"Bearer {token}"}
    created = await client.post(
        "/api/projects",
        headers=auth,
        json={
            "name": "Impact-aware segmentation",
            "statement": "Study structural response with vision-based measurements.",
            "research_mode": "interdisciplinary",
        },
    )
    assert created.status_code == 201, created.text
    project_id = created.json()["id"]
    suggestion = await client.post(
        "/api/projects/interdisciplinary-scope/suggest",
        headers=auth,
        json={
            "name": "SAM3-assisted impact response",
            "statement": "Use SAM3 segmentation to study structural response under impact load.",
        },
    )
    assert suggestion.status_code == 200, suggestion.text
    assert suggestion.json()["primary_domain"] != "Pending"
    assert suggestion.json()["related_domains"]
    scope = {
        "research_scope": (
            "Use segmentation observations to explain structural response under dynamic impact."
        ),
        "core_questions": ["Which visual measurements are mechanically meaningful?"],
        "primary_domain": "Structural engineering",
        "related_domains": ["Computer vision", "Data-driven mechanics"],
        "evidence_boundary": (
            "Only claims supported by the selected library and validated experiments."
        ),
        "validation_conditions": ["Compare predicted and measured displacement fields."],
        "user_questions": [],
    }
    saved = await client.put(
        f"/api/projects/{project_id}/interdisciplinary/scope", headers=auth, json=scope
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["status"] == "draft"
    revised = await client.put(
        f"/api/projects/{project_id}/interdisciplinary/scope",
        headers=auth,
        json={**scope, "research_scope": scope["research_scope"] + " Revised boundary."},
    )
    assert revised.status_code == 200, revised.text
    assert revised.json()["version"] == 2
    versions = await client.get(
        f"/api/projects/{project_id}/interdisciplinary/scope/versions", headers=auth
    )
    assert versions.status_code == 200, versions.text
    assert [item["version"] for item in versions.json()] == [2, 1]
    assert versions.json()[1]["research_scope"] == scope["research_scope"]
    confirmed = await client.post(
        f"/api/projects/{project_id}/interdisciplinary/scope/confirm", headers=auth
    )
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["profile"]["status"] == "confirmed"
    library_id = confirmed.json()["library_id"]

    repeated = await client.post(
        f"/api/projects/{project_id}/interdisciplinary/scope/confirm", headers=auth
    )
    assert repeated.status_code == 200, repeated.text
    assert repeated.json()["library_id"] == library_id

    libraries = await client.get(f"/api/projects/{project_id}/source-libraries", headers=auth)
    assert libraries.status_code == 200, libraries.text
    row = next(item for item in libraries.json() if item["id"] == library_id)
    assert row["library_kind"] == "interdisciplinary"
    assert row["interdisciplinary_domains"] == [
        "Structural engineering",
        "Computer vision",
        "Data-driven mechanics",
    ]

    run = await client.post(
        f"/api/libraries/{library_id}/literature/runs",
        headers=auth,
        json={
            "requested_count": 50,
            "candidate_budget": 80,
            "start_year": 2016,
            "end_year": 2026,
            "topic": "vision measurements for structural impact response",
            "source_config": {
                "sources": ["openalex", "pubmed"],
                "keywords": ["dynamic impact", "segmentation"],
            },
        },
    )
    assert run.status_code == 201, run.text
    body = run.json()
    assert body["requested_count"] == 50
    assert body["candidate_budget"] == 80
    assert body["start_year"] == 2016
    expected_version = confirmed.json()["profile"]["version"]
    assert body["query_plan"]["interdisciplinary"]["profile_version"] == expected_version
    queries = body["query_plan"]["queries"]
    assert {item["source"] for item in queries} == {"openalex", "pubmed"}
    assert {item["role"] for item in queries} >= {"primary", "related", "bridge"}


@pytest.mark.asyncio
async def test_interdisciplinary_scope_is_owner_managed(client):
    owner_token = await register_and_login(client, email="interdisciplinary-owner-2@example.com")
    other_token = await register_and_login(client, email="interdisciplinary-viewer@example.com")
    project = await client.post(
        "/api/projects",
        headers={"Authorization": f"Bearer {owner_token}"},
        json={"name": "Private cross domain topic", "research_mode": "interdisciplinary"},
    )
    project_id = project.json()["id"]
    response = await client.put(
        f"/api/projects/{project_id}/interdisciplinary/scope",
        headers={"Authorization": f"Bearer {other_token}"},
        json={
            "research_scope": "An unauthorized profile must not be writable.",
            "core_questions": ["Q"],
            "primary_domain": "Engineering",
            "related_domains": ["Computing"],
        },
    )
    assert response.status_code in {403, 404}


async def test_confirmation_reuses_an_existing_dedicated_library(client):
    """课题下已经有跨学科库时，确认要复用它，且不会多出第二个。

    注意这条**没有**覆盖 confirm 里的 except IntegrityError 分支，而且覆盖不了：
    唯一索引的条件（interdisciplinary_project_id + library_kind）与 handler 先做的
    那次查询完全一致，所以任何能撞索引的行都会先被查到、走 else 更新分支。那条
    恢复路径只有真并发（两个请求都查到 None）才够得着，单进程测试进不去。
    留着这段说明，是免得后来者看到用例名就以为并发路径有人守着。
    """
    import uuid as _uuid

    from app.core.db import get_sessionmaker
    from app.models.library_direction import DirectionLibrary

    token = await register_and_login(client, email="interdisciplinary-race@example.com")
    auth = {"Authorization": f"Bearer {token}"}
    created = await client.post(
        "/api/projects",
        headers=auth,
        json={
            "name": "Race condition project",
            "statement": "Study structural response with vision measurements.",
            "research_mode": "interdisciplinary",
        },
    )
    project_id = created.json()["id"]
    saved = await client.put(
        f"/api/projects/{project_id}/interdisciplinary/scope",
        headers=auth,
        json={
            "research_scope": "Study coupled structural and visual evidence.",
            "core_questions": ["Which measurements are meaningful?"],
            "primary_domain": "Structural engineering",
            "related_domains": ["Computer vision"],
            "evidence_boundary": None,
            "validation_conditions": [],
            "user_questions": [],
        },
    )
    assert saved.status_code == 200, saved.text

    # 抢先占住唯一索引，模拟另一个并发请求已经建好了库
    async with get_sessionmaker()() as session:
        squatter = DirectionLibrary(
            name="already there",
            statement="Pre-existing cross-disciplinary library.",
            library_kind="interdisciplinary",
            interdisciplinary_project_id=_uuid.UUID(project_id),
        )
        session.add(squatter)
        await session.commit()
        squatter_id = str(squatter.id)

    confirmed = await client.post(
        f"/api/projects/{project_id}/interdisciplinary/scope/confirm", headers=auth
    )
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["library_id"] == squatter_id
    assert confirmed.json()["profile"]["status"] == "confirmed"

    async with get_sessionmaker()() as session:
        from sqlalchemy import func, select

        count = await session.scalar(
            select(func.count())
            .select_from(DirectionLibrary)
            .where(
                DirectionLibrary.interdisciplinary_project_id == _uuid.UUID(project_id),
                DirectionLibrary.library_kind == "interdisciplinary",
            )
        )
    assert count == 1, f"课题下应只剩一个跨学科库，实际 {count}"


async def test_concurrent_confirmation_recovers_instead_of_500(client, monkeypatch):
    """两个请求都查到 None 时，唯一索引撞车要能兜回已有的库，而不是 500。

    这一幕单靠外部请求造不出来：唯一索引的条件和 handler 那次查询完全一致，
    任何能撞索引的行都会先被查到。所以这里把查询打桩成 None，复现并发下
    「两边都以为没有库、都去建」的那一刻。

    这条路径原本是坏的：rollback 会让 project 过期，之后再读 project.id 会在同步
    属性访问里触发异步 IO，抛 MissingGreenlet —— 为并发写的幂等兜底，真到并发时
    自己先炸。
    """
    import uuid as _uuid

    from app.api import interdisciplinary as api
    from app.core.db import get_sessionmaker
    from app.models.library_direction import DirectionLibrary

    token = await register_and_login(client, email="interdisciplinary-race@example.com")
    auth = {"Authorization": f"Bearer {token}"}
    created = await client.post(
        "/api/projects",
        headers=auth,
        json={
            "name": "Race condition project",
            "statement": "Study structural response with vision measurements.",
            "research_mode": "interdisciplinary",
        },
    )
    project_id = created.json()["id"]
    saved = await client.put(
        f"/api/projects/{project_id}/interdisciplinary/scope",
        headers=auth,
        json={
            "research_scope": "Study coupled structural and visual evidence.",
            "core_questions": ["Which measurements are meaningful?"],
            "primary_domain": "Structural engineering",
            "related_domains": ["Computer vision"],
            "evidence_boundary": None,
            "validation_conditions": [],
            "user_questions": [],
        },
    )
    assert saved.status_code == 200, saved.text

    async with get_sessionmaker()() as session:
        winner = DirectionLibrary(
            name="won the race",
            statement="Pre-existing cross-disciplinary library.",
            library_kind="interdisciplinary",
            interdisciplinary_project_id=_uuid.UUID(project_id),
        )
        session.add(winner)
        await session.commit()
        winner_id = str(winner.id)

    real_lookup = api._existing_dedicated_library
    calls = {"n": 0}

    async def blind_first_lookup(session, pid):
        calls["n"] += 1
        if calls["n"] == 1:
            return None  # 并发对手还没提交时看到的样子
        return await real_lookup(session, pid)

    monkeypatch.setattr(api, "_existing_dedicated_library", blind_first_lookup)

    confirmed = await client.post(
        f"/api/projects/{project_id}/interdisciplinary/scope/confirm", headers=auth
    )
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["library_id"] == winner_id
    assert calls["n"] == 2, "应当走过一次恢复性重查"
