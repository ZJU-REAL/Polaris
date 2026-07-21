"""管理端 LLM 配置测试：providers CRUD / routes / usage 聚合 / test-model / 权限。"""

import asyncio
import uuid

import httpx
import respx
from sqlalchemy import select

from app.core.db import get_sessionmaker
from app.core.llm.fake import FakeProvider
from app.models.llm_config import LLMCallLog, LLMUsage
from app.services import llm_admin
from tests.conftest import register_and_login

API_KEY = "sk-abcdef1234567890abcd"


async def _admin_and_member(client):
    admin_token = await register_and_login(client, email="admin@example.com")  # 首个 → admin
    member_token = await register_and_login(client, email="member@example.com")
    return (
        {"Authorization": f"Bearer {admin_token}"},
        {"Authorization": f"Bearer {member_token}"},
    )


async def test_admin_llm_requires_admin_role(client):
    admin, member = await _admin_and_member(client)
    for method, url, body in [
        ("GET", "/api/admin/llm/providers", None),
        ("POST", "/api/admin/llm/providers", {"name": "x", "kind": "fake"}),
        ("GET", "/api/admin/llm/routes", None),
        ("PUT", "/api/admin/llm/routes", []),
        ("GET", "/api/admin/llm/usage", None),
    ]:
        resp = await client.request(method, url, json=body, headers=member)
        assert resp.status_code == 403, (method, url, resp.status_code)
    resp = await client.get("/api/admin/llm/providers", headers=admin)
    assert resp.status_code == 200


async def test_provider_crud_and_key_masking(client):
    admin, _ = await _admin_and_member(client)

    resp = await client.post(
        "/api/admin/llm/providers",
        json={
            "name": "deepseek",
            "kind": "openai_compat",
            "base_url": "https://api.deepseek.com/v1",
            "api_key": API_KEY,
            "enabled": True,
        },
        headers=admin,
    )
    assert resp.status_code == 201, resp.text
    provider = resp.json()
    assert provider["api_key_masked"] == "sk-...abcd"  # 只写不读
    assert "api_key" not in provider
    provider_id = provider["id"]

    # 空 api_key = 不变
    resp = await client.patch(
        f"/api/admin/llm/providers/{provider_id}",
        json={"api_key": "", "enabled": False},
        headers=admin,
    )
    assert resp.json()["api_key_masked"] == "sk-...abcd"
    assert resp.json()["enabled"] is False

    # 换 key → 掩码变化
    resp = await client.patch(
        f"/api/admin/llm/providers/{provider_id}",
        json={"api_key": "sk-zzzzzzzzzzzzzzzz9999"},
        headers=admin,
    )
    assert resp.json()["api_key_masked"] == "sk-...9999"

    # 重名 → 409
    resp = await client.post(
        "/api/admin/llm/providers", json={"name": "deepseek", "kind": "fake"}, headers=admin
    )
    assert resp.status_code == 409

    resp = await client.delete(f"/api/admin/llm/providers/{provider_id}", headers=admin)
    assert resp.status_code == 204
    resp = await client.get("/api/admin/llm/providers", headers=admin)
    assert resp.json() == []


async def test_provider_models_list_roundtrip(client):
    admin, _ = await _admin_and_member(client)

    # 创建时不带 models → None
    resp = await client.post(
        "/api/admin/llm/providers",
        json={"name": "relay", "kind": "openai_compat", "base_url": "http://relay.test/api/v1"},
        headers=admin,
    )
    assert resp.status_code == 201, resp.text
    provider = resp.json()
    assert provider["models"] is None
    provider_id = provider["id"]

    # PATCH 设置 models
    models = ["gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna", "gpt-5.5"]
    resp = await client.patch(
        f"/api/admin/llm/providers/{provider_id}", json={"models": models}, headers=admin
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["models"] == models

    # 不带 models 的 PATCH 不改动
    resp = await client.patch(
        f"/api/admin/llm/providers/{provider_id}", json={"enabled": False}, headers=admin
    )
    assert resp.json()["models"] == models

    # PATCH 整体替换
    resp = await client.patch(
        f"/api/admin/llm/providers/{provider_id}", json={"models": ["gpt-5.5"]}, headers=admin
    )
    assert resp.json()["models"] == ["gpt-5.5"]

    # 创建时带 models；列表接口也返回
    resp = await client.post(
        "/api/admin/llm/providers",
        json={"name": "relay2", "kind": "openai_compat", "models": ["m-a", "m-b"]},
        headers=admin,
    )
    assert resp.status_code == 201
    assert resp.json()["models"] == ["m-a", "m-b"]
    resp = await client.get("/api/admin/llm/providers", headers=admin)
    by_name = {p["name"]: p for p in resp.json()}
    assert by_name["relay"]["models"] == ["gpt-5.5"]
    assert by_name["relay2"]["models"] == ["m-a", "m-b"]


async def test_routes_put_get_and_validation(client):
    admin, _ = await _admin_and_member(client)
    resp = await client.post(
        "/api/admin/llm/providers", json={"name": "fake", "kind": "fake"}, headers=admin
    )
    provider_id = resp.json()["id"]

    routes = [
        {"stage": "default", "provider_id": provider_id, "model": "fake-cheap"},
        {
            "stage": "navigator",
            "provider_id": provider_id,
            "model": "fake-strong",
            "temperature": 0.2,
        },
    ]
    resp = await client.put("/api/admin/llm/routes", json=routes, headers=admin)
    assert resp.status_code == 200, resp.text
    got = {r["stage"]: r for r in resp.json()}
    assert got["default"]["model"] == "fake-cheap"
    # 未显式给 temperature 时为 None（= 不向模型发送该参数，新款 Claude 已弃用它）
    assert got["default"]["temperature"] is None
    assert got["navigator"]["temperature"] == 0.2

    resp = await client.get("/api/admin/llm/routes", headers=admin)
    assert len(resp.json()) == 2

    # 非法 stage → 400
    resp = await client.put(
        "/api/admin/llm/routes",
        json=[{"stage": "nope", "provider_id": provider_id, "model": "m"}],
        headers=admin,
    )
    assert resp.status_code == 400

    # 不存在的 provider → 400
    resp = await client.put(
        "/api/admin/llm/routes",
        json=[{"stage": "default", "provider_id": str(uuid.uuid4()), "model": "m"}],
        headers=admin,
    )
    assert resp.status_code == 400

    # 整表覆盖：PUT 空表清空
    resp = await client.put("/api/admin/llm/routes", json=[], headers=admin)
    assert resp.json() == []


async def test_routes_partial_put_deletes_missing_rows(client):
    """路由行是可选的：只提交部分 stage，未提交的行被删掉（该环节回退 default）。"""
    admin, _ = await _admin_and_member(client)
    resp = await client.post(
        "/api/admin/llm/providers", json={"name": "fake", "kind": "fake"}, headers=admin
    )
    provider_id = resp.json()["id"]

    full = [
        {"stage": "default", "provider_id": provider_id, "model": "fake-cheap"},
        {"stage": "embedding", "provider_id": provider_id, "model": "fake-embed"},
        {"stage": "rerank", "provider_id": provider_id, "model": "fake-rerank"},
    ]
    resp = await client.put("/api/admin/llm/routes", json=full, headers=admin)
    assert resp.status_code == 200, resp.text
    assert len(resp.json()) == 3

    # 只提交 default + embedding → rerank 行被删（回退默认）
    resp = await client.put("/api/admin/llm/routes", json=full[:2], headers=admin)
    assert resp.status_code == 200
    assert {r["stage"] for r in resp.json()} == {"default", "embedding"}
    resp = await client.get("/api/admin/llm/routes", headers=admin)
    assert {r["stage"] for r in resp.json()} == {"default", "embedding"}


# ---- test-model ----


async def _fake_provider_id(client, admin) -> str:
    resp = await client.post(
        "/api/admin/llm/providers", json={"name": "fake", "kind": "fake"}, headers=admin
    )
    assert resp.status_code == 201
    return resp.json()["id"]


async def test_test_model_requires_admin(client):
    admin, member = await _admin_and_member(client)
    provider_id = await _fake_provider_id(client, admin)
    body = {"provider_id": provider_id, "model": "fake-default", "capability": "chat"}
    resp = await client.post("/api/admin/llm/test-model", json=body, headers=member)
    assert resp.status_code == 403


async def test_test_model_provider_not_found(client):
    admin, _ = await _admin_and_member(client)
    body = {"provider_id": str(uuid.uuid4()), "model": "m", "capability": "chat"}
    resp = await client.post("/api/admin/llm/test-model", json=body, headers=admin)
    assert resp.status_code == 404


async def test_test_model_fake_all_capabilities_no_accounting(client):
    """fake provider 三种 capability 全通；探测不写 LLMUsage 记账、不写调用日志。"""
    admin, _ = await _admin_and_member(client)
    provider_id = await _fake_provider_id(client, admin)
    # 打开调用日志开关，验证探测调用依然不落日志
    resp = await client.put(
        "/api/admin/llm/call-logs/settings", json={"enabled": True}, headers=admin
    )
    assert resp.status_code == 200

    for capability in ("chat", "embedding", "rerank"):
        resp = await client.post(
            "/api/admin/llm/test-model",
            json={"provider_id": provider_id, "model": "fake-default", "capability": capability},
            headers=admin,
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["ok"] is True, (capability, data)
        assert data["latency_ms"] >= 1
        assert data["error"] is None

    async with get_sessionmaker()() as session:
        assert (await session.execute(select(LLMUsage))).scalars().all() == []
        assert (await session.execute(select(LLMCallLog))).scalars().all() == []


async def _openai_provider_id(client, admin) -> str:
    resp = await client.post(
        "/api/admin/llm/providers",
        json={
            "name": "relay",
            "kind": "openai_compat",
            "base_url": "http://relay.test/v1",
            "api_key": API_KEY,
        },
        headers=admin,
    )
    assert resp.status_code == 201
    return resp.json()["id"]


@respx.mock
async def test_test_model_openai_compat_chat(client):
    admin, _ = await _admin_and_member(client)
    provider_id = await _openai_provider_id(client, admin)
    route = respx.post("http://relay.test/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={
                "model": "gpt-5.5",
                "choices": [{"message": {"content": "pong"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )
    )
    resp = await client.post(
        "/api/admin/llm/test-model",
        json={"provider_id": provider_id, "model": "gpt-5.5", "capability": "chat"},
        headers=admin,
    )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    # 最小探测：ping + max_tokens ≤ 8
    import json as jsonlib

    payload = jsonlib.loads(route.calls.last.request.content)
    assert payload["model"] == "gpt-5.5"
    assert payload["max_tokens"] <= 8
    assert payload["messages"] == [{"role": "user", "content": "ping"}]


@respx.mock
async def test_test_model_openai_compat_embedding_and_rerank(client):
    admin, _ = await _admin_and_member(client)
    provider_id = await _openai_provider_id(client, admin)
    respx.post("http://relay.test/v1/embeddings").mock(
        return_value=httpx.Response(
            200, json={"data": [{"index": 0, "embedding": [0.1, 0.2, 0.3]}]}
        )
    )
    respx.post("http://relay.test/v1/rerank").mock(
        return_value=httpx.Response(200, json={"results": [{"index": 0, "relevance_score": 0.9}]})
    )
    for capability, model in (("embedding", "bge-m3"), ("rerank", "bge-reranker-v2-m3")):
        resp = await client.post(
            "/api/admin/llm/test-model",
            json={"provider_id": provider_id, "model": model, "capability": capability},
            headers=admin,
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["ok"] is True, (capability, resp.json())


@respx.mock
async def test_test_model_error_reported(client):
    admin, _ = await _admin_and_member(client)
    provider_id = await _openai_provider_id(client, admin)
    respx.post("http://relay.test/v1/chat/completions").mock(
        return_value=httpx.Response(401, json={"error": "bad key"})
    )
    resp = await client.post(
        "/api/admin/llm/test-model",
        json={"provider_id": provider_id, "model": "gpt-5.5", "capability": "chat"},
        headers=admin,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is False
    assert "401" in (data["error"] or "")


async def test_test_model_embedding_not_supported(client):
    """anthropic 不支持 embedding → ok=False 且错误可读（NotImplementedError）。"""
    admin, _ = await _admin_and_member(client)
    resp = await client.post(
        "/api/admin/llm/providers",
        json={"name": "claude", "kind": "anthropic", "api_key": API_KEY},
        headers=admin,
    )
    provider_id = resp.json()["id"]
    resp = await client.post(
        "/api/admin/llm/test-model",
        json={"provider_id": provider_id, "model": "claude-fable-5", "capability": "embedding"},
        headers=admin,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is False
    assert "NotImplementedError" in data["error"]


async def test_test_model_timeout(client, monkeypatch):
    admin, _ = await _admin_and_member(client)
    provider_id = await _fake_provider_id(client, admin)

    async def slow_complete(self, messages, **kwargs):  # noqa: ANN001, ANN003
        await asyncio.sleep(5)

    monkeypatch.setattr(llm_admin, "_TEST_TIMEOUT_S", 0.05)
    monkeypatch.setattr(FakeProvider, "complete", slow_complete)
    resp = await client.post(
        "/api/admin/llm/test-model",
        json={"provider_id": provider_id, "model": "fake-default", "capability": "chat"},
        headers=admin,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is False
    assert "timeout" in data["error"]


async def test_usage_aggregation(client):
    admin, _ = await _admin_and_member(client)
    user_id = uuid.uuid4()
    async with get_sessionmaker()() as session:
        for _i in range(3):
            session.add(
                LLMUsage(
                    user_id=None,
                    project_id=None,
                    voyage_id=None,
                    stage="navigator",
                    model="fake-default",
                    prompt_tokens=100,
                    completion_tokens=50,
                )
            )
        session.add(
            LLMUsage(
                user_id=None,
                project_id=None,
                voyage_id=None,
                stage="sextant",
                model="fake-default",
                prompt_tokens=10,
                completion_tokens=5,
            )
        )
        await session.commit()

    resp = await client.get("/api/admin/llm/usage?days=7", headers=admin)
    assert resp.status_code == 200
    rows = {r["stage"]: r for r in resp.json()}
    assert rows["navigator"]["prompt_tokens"] == 300
    assert rows["navigator"]["completion_tokens"] == 150
    assert rows["navigator"]["calls"] == 3
    assert rows["sextant"]["calls"] == 1

    # user 过滤（无匹配 → 空）
    resp = await client.get(f"/api/admin/llm/usage?user_id={user_id}", headers=admin)
    assert resp.json() == []
