"""DeepSeek Harness integration: scoped tokens, native skills, and MCP profiles."""

import uuid

from sqlalchemy import select

from app.core.db import get_sessionmaker
from app.integrations.deepseek_harness.profile import FULL_PROFILE, READONLY_PROFILE
from app.models.agent_skill import AgentSkill
from app.models.integration_token import IntegrationToken
from app.models.user import User
from app.services import agent_skills
from tests.conftest import register_and_login


async def _user(client, email: str) -> tuple[str, uuid.UUID]:
    jwt = await register_and_login(client, email=email)
    async with get_sessionmaker()() as session:
        user_id = await session.scalar(select(User.id).where(User.email == email))
    assert user_id is not None
    return jwt, user_id


async def _integration_token(
    client,
    jwt: str,
    *,
    scopes: list[str],
    name: str = "DeepSeek Harness",
) -> dict:
    response = await client.post(
        "/api/integration-tokens",
        json={"name": name, "scopes": scopes, "expires_in_days": 30},
        headers={"Authorization": f"Bearer {jwt}"},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _headers(raw_token: str, *, profile: str | None = None) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {raw_token}"}
    if profile is not None:
        headers["X-Polaris-Tool-Profile"] = profile
    return headers


async def _put_skill(
    *,
    user_id: uuid.UUID | None,
    slug: str,
    body: str,
    scope: str = "user",
    invocation: str = "auto",
    tools: str = "search_papers, get_paper",
    files: dict[str, str] | None = None,
) -> AgentSkill:
    text = f"""\
---
name: {slug}
title: {slug} title
description: Use when the user needs {slug}.
allowed-tools: [{tools}]
invocation: {invocation}
---

{body}
"""
    async with get_sessionmaker()() as session:
        skill = await agent_skills.upsert_from_md(
            session,
            text=text,
            user_id=user_id,
            scope=scope,
            files=files,
        )
        await session.commit()
        return skill


def test_dsh_profiles_are_declared_in_the_adapter_layer():
    assert READONLY_PROFILE.name == "dsh-readonly-v1"
    assert READONLY_PROFILE.include_writes is False
    assert FULL_PROFILE.name == "dsh-full-v1"
    assert FULL_PROFILE.include_writes is True


async def test_integration_token_is_scoped_one_time_and_revocable(client):
    jwt, _ = await _user(client, "dsh-token@example.com")
    created = await _integration_token(client, jwt, scopes=["skills:read"])

    assert created["token"].startswith("polaris_it_")
    assert created["token_prefix"] == created["token"][:20]
    async with get_sessionmaker()() as session:
        stored = await session.get(IntegrationToken, uuid.UUID(created["id"]))
        assert stored is not None
        assert stored.token_hash != created["token"]
        assert len(stored.token_hash) == 64
    listing = await client.get(
        "/api/integration-tokens",
        headers={"Authorization": f"Bearer {jwt}"},
    )
    assert listing.status_code == 200
    assert len(listing.json()) == 1
    assert "token" not in listing.json()[0]
    assert "token_hash" not in listing.json()[0]

    skills = await client.get(
        "/api/integrations/deepseek-harness/v1/skills",
        headers=_headers(created["token"]),
    )
    assert skills.status_code == 200
    async with get_sessionmaker()() as session:
        read_only_use = await session.get(IntegrationToken, uuid.UUID(created["id"]))
        assert read_only_use is not None
        assert read_only_use.last_used_at is None
    mcp = await client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        headers=_headers(created["token"]),
    )
    assert mcp.status_code == 403

    other_jwt, _ = await _user(client, "dsh-token-other@example.com")
    wrong_owner = await client.delete(
        f"/api/integration-tokens/{created['id']}",
        headers={"Authorization": f"Bearer {other_jwt}"},
    )
    assert wrong_owner.status_code == 404

    revoked = await client.delete(
        f"/api/integration-tokens/{created['id']}",
        headers={"Authorization": f"Bearer {jwt}"},
    )
    assert revoked.status_code == 204
    rejected = await client.get(
        "/api/integrations/deepseek-harness/v1/skills",
        headers=_headers(created["token"]),
    )
    assert rejected.status_code == 401

    blank = await client.post(
        "/api/integration-tokens",
        json={"name": "   ", "scopes": ["skills:read"]},
        headers={"Authorization": f"Bearer {jwt}"},
    )
    assert blank.status_code == 422
    invalid_scopes = await client.post(
        "/api/integration-tokens",
        json={"name": "bad", "scopes": ["mcp:write"]},
        headers={"Authorization": f"Bearer {jwt}"},
    )
    assert invalid_scopes.status_code == 422


async def test_native_skill_catalog_isolated_shadowed_and_cacheable(client):
    jwt, user_id = await _user(client, "dsh-skills@example.com")
    _, other_user_id = await _user(client, "dsh-skills-other@example.com")
    token = await _integration_token(client, jwt, scopes=["skills:read"])

    await _put_skill(
        user_id=None,
        slug="review-papers",
        body="# Builtin body",
        scope="builtin",
    )
    await _put_skill(
        user_id=user_id,
        slug="review-papers",
        body="# User body",
        invocation="manual",
        tools="search_papers",
        files={"references/rubric.md": "# Rubric\nKeep relevant work."},
    )
    await _put_skill(
        user_id=other_user_id,
        slug="private-skill",
        body="# Other user's body",
    )

    endpoint = "/api/integrations/deepseek-harness/v1/skills"
    response = await client.get(endpoint, headers=_headers(token["token"]))
    assert response.status_code == 200, response.text
    etag = response.headers["etag"]
    catalog = response.json()
    assert [skill["slug"] for skill in catalog["skills"]] == ["review-papers"]
    item = catalog["skills"][0]
    assert item["scope"] == "user"
    assert item["invocation"] == "manual"
    assert item["allowedTools"] == ["search_papers"]
    assert item["files"][0]["path"] == "references/rubric.md"
    assert item["updatedAt"].endswith(("Z", "+00:00"))
    assert "body" not in item

    detail = await client.get(f"{endpoint}/review-papers", headers=_headers(token["token"]))
    assert detail.status_code == 200
    assert detail.json()["body"] == "# User body"
    detail_etag = detail.headers["etag"]
    unchanged_detail = await client.get(
        f"{endpoint}/review-papers",
        headers={**_headers(token["token"]), "If-None-Match": detail_etag},
    )
    assert unchanged_detail.status_code == 304

    unchanged_catalog = await client.get(
        endpoint,
        headers={**_headers(token["token"]), "If-None-Match": etag},
    )
    assert unchanged_catalog.status_code == 304

    attachment = await client.get(
        f"{endpoint}/review-papers/files/references/rubric.md",
        headers=_headers(token["token"]),
    )
    assert attachment.status_code == 200
    assert attachment.text.startswith("# Rubric")
    assert attachment.headers["x-content-type-options"] == "nosniff"
    missing = await client.get(
        f"{endpoint}/review-papers/files/references/missing.md",
        headers=_headers(token["token"]),
    )
    assert missing.status_code == 404


async def test_dsh_mcp_profiles_enforce_discovery_and_direct_calls(client):
    jwt, user_id = await _user(client, "dsh-mcp@example.com")
    read_token = await _integration_token(client, jwt, scopes=["mcp:read"])
    full_token = await _integration_token(
        client,
        jwt,
        scopes=["mcp:read", "mcp:write"],
        name="DeepSeek Harness full",
    )
    request = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}

    readonly = await client.post(
        "/mcp",
        json=request,
        headers=_headers(read_token["token"], profile="dsh-readonly-v1"),
    )
    assert readonly.status_code == 200, readonly.text
    readonly_names = {tool["name"] for tool in readonly.json()["result"]["tools"]}
    assert "search_papers" in readonly_names
    assert "remember" not in readonly_names
    assert {
        "run_subagent",
        "skill_load",
        "skill_read_file",
        "submit_plan",
        "update_plan",
    }.isdisjoint(readonly_names)

    direct_call = await client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "skill_load", "arguments": {"slug": "anything"}},
        },
        headers=_headers(read_token["token"], profile="dsh-readonly-v1"),
    )
    assert direct_call.status_code == 200
    result = direct_call.json()["result"]
    assert result["isError"] is True
    assert "dsh-readonly-v1" in result["content"][0]["text"]

    denied_full = await client.post(
        "/mcp",
        json=request,
        headers=_headers(read_token["token"], profile="dsh-full-v1"),
    )
    assert denied_full.status_code == 403

    full = await client.post(
        "/mcp",
        json=request,
        headers=_headers(full_token["token"], profile="dsh-full-v1"),
    )
    assert full.status_code == 200, full.text
    full_names = {tool["name"] for tool in full.json()["result"]["tools"]}
    assert "remember" in full_names
    remembered = await client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "remember",
                "arguments": {"text": "Prefer concise summaries", "kind": "fact"},
            },
        },
        headers=_headers(full_token["token"], profile="dsh-full-v1"),
    )
    assert remembered.status_code == 200
    assert remembered.json()["result"]["isError"] is False, remembered.text

    invalid_profile = await client.post(
        "/mcp",
        json=request,
        headers=_headers(read_token["token"], profile="unknown"),
    )
    assert invalid_profile.status_code == 400

    async with get_sessionmaker()() as session:
        user = await session.get(User, user_id)
        assert user is not None
        user.read_only = True
        await session.commit()
    read_only_account = await client.post(
        "/mcp",
        json=request,
        headers=_headers(full_token["token"], profile="dsh-full-v1"),
    )
    assert read_only_account.status_code == 403
