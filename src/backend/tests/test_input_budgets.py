"""按环节配置的输入预算（#811）。

要钉住的几件事：

- 不配置的人行为不变——登记的默认值就是原来写死的常量；
- 配置值被上下限夹住，再被模型窗口封顶；
- 设置页存进去的值真的到了调用处（「存了但不生效」是这类改动最常见的死法）；
- ``context_window`` 能存能读——以前 API 上没有这个字段，而 PUT 是整表覆盖，
  于是每保存一次路由表，填过的窗口就被悄悄清空。
"""

import uuid

import pytest

from app.core.llm.budgets import (
    CHARS_PER_WINDOW_TOKEN,
    FORGE_CONTEXT,
    FORGE_EXCERPT,
    INPUT_BUDGETS,
    LIBRARIAN_FULLTEXT,
    budgets_for,
    resolve_budget,
    validate_budgets,
)
from app.core.llm.router import get_llm_router
from app.models.paper import Paper
from app.services.wiki_compile import build_compile_prompt
from tests.conftest import register_and_login

# ---- 登记表 ----


def test_defaults_are_the_constants_they_replaced():
    """默认值一变，所有没配置的人的结果就跟着变了——升级不该改变任何人的默认行为。"""
    assert LIBRARIAN_FULLTEXT.default == 24_000
    assert FORGE_CONTEXT.default == 12_000
    assert FORGE_EXCERPT.default == 800


def test_every_default_sits_inside_its_own_bounds():
    for b in INPUT_BUDGETS:
        assert b.minimum <= b.default <= b.maximum, b


def test_keys_are_unique_per_stage():
    seen = {(b.stage, b.key) for b in INPUT_BUDGETS}
    assert len(seen) == len(INPUT_BUDGETS)


# ---- 生效值 ----


def test_unset_means_default():
    assert LIBRARIAN_FULLTEXT.effective(None, None) == 24_000


def test_configured_value_is_clamped_into_bounds():
    assert LIBRARIAN_FULLTEXT.effective(1, None) == LIBRARIAN_FULLTEXT.minimum
    assert LIBRARIAN_FULLTEXT.effective(10**9, None) == LIBRARIAN_FULLTEXT.maximum
    assert LIBRARIAN_FULLTEXT.effective(60_000, None) == 60_000


def test_the_context_window_caps_the_budget():
    window = 16_000  # token
    cap = window * CHARS_PER_WINDOW_TOKEN
    assert LIBRARIAN_FULLTEXT.effective(100_000, window) == cap
    # 默认值也要被封顶：8k 窗口的模型塞 24000 字只会被服务端拒掉或截断
    assert LIBRARIAN_FULLTEXT.effective(None, 8_000) == 8_000 * CHARS_PER_WINDOW_TOKEN


def test_a_tiny_window_wins_over_the_minimum():
    """窗口真的很小时，下限没有意义：塞得比窗口还多只会失败。"""
    assert LIBRARIAN_FULLTEXT.effective(None, 1_024) == 1_024 * CHARS_PER_WINDOW_TOKEN
    assert LIBRARIAN_FULLTEXT.minimum > 1_024 * CHARS_PER_WINDOW_TOKEN


# ---- 保存时校验 ----


def test_valid_budgets_pass():
    validate_budgets("forge", {"context_chars": 80_000, "excerpt_chars": 2_000}, None)
    validate_budgets("librarian", None, None)
    validate_budgets("librarian", {}, 8_000)


def test_unknown_key_is_rejected_not_dropped():
    with pytest.raises(ValueError, match="no input budget named"):
        validate_budgets("librarian", {"context_chars": 50_000}, None)
    # 没有登记任何预算的环节也一样
    with pytest.raises(ValueError, match="no input budget named"):
        validate_budgets("navigator", {"fulltext_chars": 50_000}, None)


def test_out_of_range_is_rejected():
    with pytest.raises(ValueError, match="between"):
        validate_budgets("librarian", {"fulltext_chars": 10}, None)


def test_a_budget_larger_than_the_window_is_rejected():
    with pytest.raises(ValueError, match="context window"):
        validate_budgets("librarian", {"fulltext_chars": 100_000}, 32_000)
    validate_budgets("librarian", {"fulltext_chars": 64_000}, 32_000)


def test_budgets_for_lists_a_stage():
    assert set(budgets_for("forge")) == {"context_chars", "excerpt_chars"}
    assert budgets_for("navigator") == {}


# ---- 调用处 ----


def test_compile_prompt_keeps_the_head_of_the_body_within_budget():
    paper = Paper(id=uuid.uuid4(), title="T", abstract="A" * 30 + "B" * 70, authors=[])
    prompt, _ = build_compile_prompt(paper, fulltext_chars=30)
    body = prompt.split("正文：\n", 1)[1]
    assert body == "A" * 30


def test_compile_prompt_default_is_unchanged():
    paper = Paper(id=uuid.uuid4(), title="T", abstract="x" * 30_000, authors=[])
    prompt, _ = build_compile_prompt(paper)
    assert prompt.split("正文：\n", 1)[1] == "x" * 24_000


async def test_test_doubles_without_budgets_get_the_default():
    """测试替身 / 没接路由表的调用方拿默认值，不能因为预算让编译本身失败。"""

    class _Stub:
        async def complete(self, *a, **k):  # pragma: no cover - 不会被调用
            raise AssertionError

    assert await resolve_budget(_Stub(), LIBRARIAN_FULLTEXT, None) == 24_000


# ---- 端到端：设置页 → 路由器 ----


async def _owner(client) -> dict[str, str]:
    token = await register_and_login(client, email="owner@example.com")
    return {"Authorization": f"Bearer {token}"}


async def _fake_provider(client, headers) -> str:
    resp = await client.post(
        "/api/admin/llm/providers", json={"name": "fake", "kind": "fake"}, headers=headers
    )
    return resp.json()["id"]


async def test_context_window_and_budgets_survive_a_save(client):
    """以前 PUT 不认 context_window，存一次路由表就把它清空。"""
    headers = await _owner(client)
    pid = await _fake_provider(client, headers)
    routes = [
        {"stage": "default", "provider_id": pid, "model": "m", "context_window": 128_000},
        {
            "stage": "librarian",
            "provider_id": pid,
            "model": "m-long",
            "context_window": 200_000,
            "input_budgets": {"fulltext_chars": 60_000},
        },
    ]
    resp = await client.put("/api/admin/llm/routes", json=routes, headers=headers)
    assert resp.status_code == 200, resp.text
    resp = await client.get("/api/admin/llm/routes", headers=headers)
    got = {r["stage"]: r for r in resp.json()}
    assert got["default"]["context_window"] == 128_000
    assert got["default"]["input_budgets"] is None
    assert got["librarian"]["context_window"] == 200_000
    assert got["librarian"]["input_budgets"] == {"fulltext_chars": 60_000}


async def test_invalid_budgets_are_refused_with_a_reason(client):
    headers = await _owner(client)
    pid = await _fake_provider(client, headers)
    cases = [
        ({"fulltext_chars": 10}, None, "between"),
        ({"nope": 5_000}, None, "no input budget named"),
        ({"fulltext_chars": 100_000}, 16_000, "context window"),
    ]
    for budgets, window, reason in cases:
        resp = await client.put(
            "/api/admin/llm/routes",
            json=[
                {
                    "stage": "librarian",
                    "provider_id": pid,
                    "model": "m",
                    "context_window": window,
                    "input_budgets": budgets,
                }
            ],
            headers=headers,
        )
        assert resp.status_code == 400, (budgets, resp.text)
        assert reason in resp.json()["detail"]


async def test_the_router_hands_out_what_settings_saved(client):
    headers = await _owner(client)
    pid = await _fake_provider(client, headers)
    routes = [
        {"stage": "default", "provider_id": pid, "model": "m", "context_window": 8_000},
        {
            "stage": "forge",
            "provider_id": pid,
            "model": "m",
            "input_budgets": {"context_chars": 80_000, "excerpt_chars": 2_000},
        },
    ]
    resp = await client.put("/api/admin/llm/routes", json=routes, headers=headers)
    assert resp.status_code == 200, resp.text

    llm = get_llm_router()
    assert await llm.input_budget(FORGE_CONTEXT) == 80_000
    assert await llm.input_budget(FORGE_EXCERPT) == 2_000
    # librarian 没有自己的行：跟随 default，用默认预算，但被 default 的窗口封顶
    assert await llm.input_budget(LIBRARIAN_FULLTEXT) == 8_000 * CHARS_PER_WINDOW_TOKEN


async def test_input_budget_spec_endpoint_lists_the_registry(client):
    headers = await _owner(client)
    resp = await client.get("/api/admin/llm/input-budgets", headers=headers)
    assert resp.status_code == 200
    got = {(b["stage"], b["key"]): b for b in resp.json()}
    assert got[("librarian", "fulltext_chars")]["default"] == 24_000
    assert got[("forge", "context_chars")]["chars_per_window_token"] == CHARS_PER_WINDOW_TOKEN
    assert len(got) == len(INPUT_BUDGETS)
