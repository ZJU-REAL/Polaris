"""插件命名空间 LLM 环节（#736）：命名校验、注册缝、三级 resolve 链、
路由表 PUT 放行、抽取 schema 自动注册与全链走通。"""

import time
import uuid

import pytest

from app.core.llm.router import (
    _FALLBACK_ROUTE,
    _LONG_CALL,
    _MEDIUM_CALL,
    _SHORT_CALL,
    STAGES,
    LLMRouter,
    ResolvedRoute,
    call_profile,
    is_plugin_stage,
    known_stages,
    plugin_stage_spec,
    register_plugin_stage,
    unregister_plugin_stage,
)


@pytest.fixture
def clean_registry():
    """逐用例登记要清场的插件环节名——运行时注册表是进程级的，漏清会污染整个套件。"""
    names: list[str] = []
    yield names
    for name in names:
        unregister_plugin_stage(name)


# ---- 1. 命名规则 ----


def test_namespace_shape_is_three_segments_of_lowercase():
    assert is_plugin_stage("plugin:pico-pack:extract-pico")
    assert is_plugin_stage("plugin:a1:b2")
    for bad in (
        "plugin:pico",  # 少一段
        "plugin:pico:extract:extra",  # 多一段
        "plugin:PICO:extract",  # 大写
        "plugin:pi_co:extract",  # 下划线
        "plugin::extract",  # 空段
        "extract_skeleton",  # 内置环节不算命名空间串
        "pluginx:pico:extract",  # 前缀必须是 plugin:
    ):
        assert not is_plugin_stage(bad), bad


def test_register_rejects_bad_segments_tier_and_fallback(clean_registry):
    with pytest.raises(ValueError, match="invalid plugin stage name"):
        register_plugin_stage("PICO", "extract")
    with pytest.raises(ValueError, match="invalid plugin stage name"):
        register_plugin_stage("pico", "ex_tract")
    # 总长超 32：stage 列都是 String(32)，写进截断串比拒绝注册糟得多
    with pytest.raises(ValueError, match="too long"):
        register_plugin_stage("a-very-long-pack-name", "a-very-long-stage")
    with pytest.raises(ValueError, match="unknown tier"):
        register_plugin_stage("pico", "extract", tier="huge")
    # fallback 只准指向内置环节：指向插件环节会形成回退链，行为取决于加载顺序
    with pytest.raises(ValueError, match="built-in"):
        register_plugin_stage("pico", "extract", fallback="plugin:other:stage")
    with pytest.raises(ValueError, match="built-in"):
        register_plugin_stage("pico", "extract", fallback="nope")


# ---- 2. 注册缝 ----


def test_register_adds_to_known_stages_and_is_idempotent(clean_registry):
    name = register_plugin_stage("pico-pack", "extract-pico", fallback="extract_skeleton")
    clean_registry.append(name)
    assert name == "plugin:pico-pack:extract-pico"
    assert name in known_stages()
    assert set(STAGES) <= known_stages()
    spec = plugin_stage_spec(name)
    assert spec is not None and spec.tier == "medium" and spec.fallback == "extract_skeleton"

    # 同名同配置幂等（schema 版本演进会重复挂同一环节）
    assert register_plugin_stage("pico-pack", "extract-pico", fallback="extract_skeleton") == name
    # 同名不同配置拒绝（两个插件抢名字 / 改档没改名，静默用先到的那份 = 配了没生效）
    with pytest.raises(ValueError, match="different config"):
        register_plugin_stage("pico-pack", "extract-pico", tier="long")

    assert unregister_plugin_stage(name)
    assert name not in known_stages()
    assert not unregister_plugin_stage(name)  # 再删无事发生


def test_call_profile_follows_the_declared_tier(clean_registry):
    for tier, profile in (("short", _SHORT_CALL), ("medium", _MEDIUM_CALL), ("long", _LONG_CALL)):
        name = register_plugin_stage("tier-pack", f"s-{tier}", tier=tier)
        clean_registry.append(name)
        assert call_profile(name) == profile
    # 内置环节的档位不受注册表影响
    assert call_profile("relevance") == _SHORT_CALL
    assert call_profile("extract_skeleton") == _MEDIUM_CALL


# ---- 3. resolve 三级链：精确 → fallback 环节路由 → default ----


def _route(model: str) -> ResolvedRoute:
    return ResolvedRoute(
        provider_kind="fake",
        base_url=None,
        api_key="",
        model=model,
        temperature=0.0,
        provider_name="fake",
    )


def _router_with(routes: dict[str, ResolvedRoute]) -> LLMRouter:
    """路由表直接塞缓存（本测试测的是选路逻辑，不是 DB 加载）。"""
    router = LLMRouter()
    router._routes = routes
    router._routes_loaded_at = time.monotonic()
    return router


async def test_resolve_prefers_the_exact_namespaced_route(clean_registry):
    name = register_plugin_stage("pico-pack", "extract-pico", fallback="extract_skeleton")
    clean_registry.append(name)
    router = _router_with(
        {
            "default": _route("m-default"),
            "extract_skeleton": _route("m-skeleton"),
            name: _route("m-exact"),
        }
    )
    _, route = await router.resolve(name)
    assert route.model == "m-exact"


async def test_resolve_falls_back_to_the_declared_builtin_stage(clean_registry):
    name = register_plugin_stage("pico-pack", "extract-pico", fallback="extract_skeleton")
    clean_registry.append(name)
    router = _router_with(
        {"default": _route("m-default"), "extract_skeleton": _route("m-skeleton")}
    )
    _, route = await router.resolve(name)
    assert route.model == "m-skeleton"


async def test_resolve_falls_back_to_default_last(clean_registry):
    name = register_plugin_stage("pico-pack", "extract-pico", fallback="extract_skeleton")
    clean_registry.append(name)
    router = _router_with({"default": _route("m-default")})
    _, route = await router.resolve(name)
    assert route.model == "m-default"

    # 没声明 fallback 的插件环节：跳过第二级，直接 default
    bare = register_plugin_stage("pico-pack", "no-fallback")
    clean_registry.append(bare)
    _, route = await router.resolve(bare)
    assert route.model == "m-default"


async def test_resolve_empty_table_uses_the_fake_fallback(clean_registry):
    """连 default 都没有：与内置环节同规则（测试套件开着 llm_fake_fallback）。"""
    name = register_plugin_stage("pico-pack", "extract-pico", fallback="extract_skeleton")
    clean_registry.append(name)
    router = _router_with({})
    _, route = await router.resolve(name)
    assert route == _FALLBACK_ROUTE


# ---- 4. 路由表 PUT：命名空间串放行 ----


async def test_routes_put_accepts_namespaced_stages(client, clean_registry):
    from tests.test_admin_llm import _admin_and_member

    admin, _ = await _admin_and_member(client)
    resp = await client.post(
        "/api/admin/llm/providers", json={"name": "fake", "kind": "fake"}, headers=admin
    )
    provider_id = resp.json()["id"]

    registered = register_plugin_stage("pico-pack", "extract-pico", fallback="extract_skeleton")
    clean_registry.append(registered)
    routes = [
        {"stage": "default", "provider_id": provider_id, "model": "fake-cheap"},
        {"stage": registered, "provider_id": provider_id, "model": "fake-pico"},
        # 未注册但形状合法的串也放行：插件卸载后的存量路由行不能把整表变成存不进去
        {"stage": "plugin:gone-pack:old-stage", "provider_id": provider_id, "model": "fake-old"},
    ]
    resp = await client.put("/api/admin/llm/routes", json=routes, headers=admin)
    assert resp.status_code == 200, resp.text
    assert {r["stage"] for r in resp.json()} == {
        "default",
        registered,
        "plugin:gone-pack:old-stage",
    }

    # 形状不合法的串照旧 400（整表拒绝）
    for bad in ("plugin:PICO:extract", "plugin:pico", "nope"):
        resp = await client.put(
            "/api/admin/llm/routes",
            json=[{"stage": bad, "provider_id": provider_id, "model": "m"}],
            headers=admin,
        )
        assert resp.status_code == 400, bad


# ---- 5. 抽取 schema：命名空间 stage 自动注册 + 全链走通 ----


PICO_PROMPT = (
    "POLARIS_EXTRACT_PICO_DEMO\n"
    "你是临床论文 PICO 抽取器。根据给定论文的标题与正文抽取：\n"
    "{fields_spec}\n"
    '只输出一个 JSON 对象，键为上述字段名，另加 "confidence"（0 到 1）。'
)


def _pico_schema():
    from app.services.extraction.schemas import EntryKey, ExtractionSchema, SchemaField

    return ExtractionSchema(
        id="pico-demo",
        version=1,
        stage="plugin:pico-pack:extract-pico",
        fields=(
            SchemaField("population", "text", max_len=200),
            SchemaField("outcomes", "list", max_len=120, max_items=4),
            SchemaField(
                "comparisons",
                "entries",
                max_len=0,
                max_items=4,
                entry_keys=(
                    EntryKey("kind", max_len=32, choices=("drug", "placebo")),
                    EntryKey("statement", max_len=200),
                ),
            ),
        ),
        prompt_template=PICO_PROMPT,
    )


def test_register_schema_auto_registers_the_namespaced_stage(clean_registry):
    from app.services.extraction import schemas as schemas_mod

    schema = _pico_schema()
    clean_registry.append(schema.stage)
    try:
        schemas_mod.register_schema(schema)
        spec = plugin_stage_spec(schema.stage)
        assert spec is not None
        # 学科 schema 与内置抽取同负载形态（整篇正文进、短 JSON 出）：中档 + 骨架回退
        assert spec.tier == "medium" and spec.fallback == "extract_skeleton"
        # 覆盖注册（版本演进）不炸：自动注册是幂等的
        schemas_mod.register_schema(schema)
    finally:
        schemas_mod._REGISTRY.pop(schema.id, None)


async def test_discipline_schema_extracts_end_to_end(client, tmp_path, clean_registry):
    """学科包只写一个 schema 就走通全链：注册 → resolve（fake 兜底）→ 抽取落库。

    fake provider 对不认识的抽取 marker 走通用兜底（按字段清单反推合法 JSON），
    所以无 key 演示/测试里学科 schema 与内置 schema 同样能跑。
    """
    from app.core.db import get_sessionmaker
    from app.services.extraction import schemas as schemas_mod
    from app.services.extraction.runtime import extract_paper
    from tests.conftest import add_paper, make_project_with_library, register_and_login
    from tests.test_paper_extraction import _write_fulltext

    schema = _pico_schema()
    clean_registry.append(schema.stage)
    try:
        schemas_mod.register_schema(schema)

        token = await register_and_login(client, email="pico@example.com")
        headers = {"Authorization": f"Bearer {token}"}
        project_id, _ = await make_project_with_library(client, headers, name="pico-proj")
        async with get_sessionmaker()() as session:
            paper = await add_paper(
                session,
                project_id=uuid.UUID(project_id),
                title="PICO Discipline Probe",
                abstract="A probe.",
                full_text_path=_write_fulltext(tmp_path),
            )
            await session.commit()
            outcome = await extract_paper(session, paper, schema_id="pico-demo")
            assert outcome.status == "extracted", outcome.reason
            row = outcome.row
            assert row.schema_id == "pico-demo"
            # 记账/产物上的 stage 是完整命名空间串
            assert row.stage_meta["stage"] == "plugin:pico-pack:extract-pico"
            # text 字段回显标题：断言 prompt 里带对了论文
            assert "PICO Discipline Probe" in row.payload["population"]
            assert row.payload["outcomes"]
            # entries 的枚举键取了合法取值，整条过了归一化
            assert row.payload["comparisons"][0]["kind"] == "drug"
            assert row.confidence == 0.7
    finally:
        schemas_mod._REGISTRY.pop(schema.id, None)
