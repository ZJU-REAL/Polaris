"""LLM 调用日志测试：开关、router 打点（complete/stream/embed/rerank）、管理 API、权限。"""

import uuid

from sqlalchemy import select

from app.core.db import get_sessionmaker
from app.core.llm import call_log
from app.core.llm.base import Message
from app.core.llm.router import LLMRouter
from app.models.llm_config import LLMCallLog
from app.models.system_setting import SystemSetting
from tests.conftest import register_and_login


async def _enable_logging() -> None:
    async with get_sessionmaker()() as session:
        session.add(SystemSetting(key=call_log.LLM_CALL_LOGGING_KEY, value=True))
        await session.commit()
    call_log.invalidate_flag_cache()


async def _all_logs() -> list[LLMCallLog]:
    async with get_sessionmaker()() as session:
        return list((await session.execute(select(LLMCallLog))).scalars().all())


# ---- router 打点 ----


async def test_complete_logged_when_enabled(app):
    await _enable_logging()
    router = LLMRouter()
    result = await router.complete("default", [Message(role="user", content="你好 Polaris")])
    assert result.content

    logs = await _all_logs()
    assert len(logs) == 1
    log = logs[0]
    assert log.stage == "default"
    assert log.provider_name == "fake"
    assert log.model == "fake-default"
    assert log.status == "ok"
    assert log.error is None
    assert log.duration_ms > 0
    assert log.request["messages"] == [{"role": "user", "content": "你好 Polaris"}]
    assert log.response == result.content
    assert log.prompt_tokens == result.usage["prompt_tokens"]
    assert log.completion_tokens == result.usage["completion_tokens"]


async def test_complete_images_never_stored_as_base64(app):
    await _enable_logging()
    router = LLMRouter()
    fake_png = b"\x89PNG" + b"x" * 5000  # ~5KB
    await router.complete("default", [Message(role="user", content="看图")], images=[fake_png])
    logs = await _all_logs()
    assert len(logs) == 1
    request = logs[0].request
    assert request["images"] == ["[image ~4 KB]"]
    # 整个 request 里绝无原始图片字节/base64
    assert "iVBOR" not in str(request)
    assert "xxxx" not in str(request)


async def test_complete_long_message_truncated(app):
    await _enable_logging()
    router = LLMRouter()
    long_text = "长" * (call_log.MESSAGE_MAX_CHARS + 500)
    await router.complete("default", [Message(role="user", content=long_text)])
    logs = await _all_logs()
    content = logs[0].request["messages"][0]["content"]
    assert len(content) < len(long_text)
    assert "[truncated" in content


async def test_stream_logged_when_enabled(app):
    await _enable_logging()
    router = LLMRouter()
    chunks = [c async for c in router.stream("default", [Message(role="user", content="流式")])]
    logs = await _all_logs()
    assert len(logs) == 1
    log = logs[0]
    assert log.status == "ok"
    assert log.duration_ms > 0
    assert log.response == "".join(chunks)  # 聚合完整输出
    assert log.request["messages"][0]["content"] == "流式"


async def test_embed_and_rerank_log_summaries(app):
    await _enable_logging()
    router = LLMRouter()
    vectors = await router.embed(["文本一", "文本二"])
    await router.rerank("查询", ["文档 A", "文档 B", "文档 C"], top_n=2)

    logs = {log.stage: log for log in await _all_logs()}
    embed_log = logs["embedding"]
    assert embed_log.request == {"texts_count": 2, "first_text": "文本一"}
    assert embed_log.response == f"[2 embeddings, dim={len(vectors[0])}]"
    assert "[" not in str(embed_log.request)  # 不存向量

    rerank_log = logs["rerank"]
    assert rerank_log.request["query"] == "查询"
    assert rerank_log.request["documents_count"] == 3
    assert rerank_log.request["first_document"] == "文档 A"
    assert rerank_log.response == "[2 rerank results]"


async def test_not_logged_when_disabled(app):
    router = LLMRouter()  # 未开开关（默认关）
    await router.complete("default", [Message(role="user", content="hi")])
    _ = [c async for c in router.stream("default", [Message(role="user", content="hi")])]
    await router.embed(["t"])
    assert await _all_logs() == []


async def test_logging_failure_does_not_break_call(app, monkeypatch):
    await _enable_logging()

    async def boom(**kwargs):
        raise RuntimeError("log sink down")

    monkeypatch.setattr(call_log, "record_call", boom)
    router = LLMRouter()
    result = await router.complete("default", [Message(role="user", content="仍应成功")])
    assert "仍应成功" in result.content
    chunks = [c async for c in router.stream("default", [Message(role="user", content="s")])]
    assert "".join(chunks)


# ---- 管理 API ----


async def _admin_and_member(client):
    admin_token = await register_and_login(client, email="admin@example.com")  # 首个 → admin
    member_token = await register_and_login(client, email="member@example.com")
    return (
        {"Authorization": f"Bearer {admin_token}"},
        {"Authorization": f"Bearer {member_token}"},
    )


async def test_call_log_endpoints_require_admin(client):
    _, member = await _admin_and_member(client)
    for method, url, body in [
        ("GET", "/api/admin/llm/call-logs", None),
        ("GET", "/api/admin/llm/call-logs/settings", None),
        ("PUT", "/api/admin/llm/call-logs/settings", {"enabled": True}),
        ("DELETE", "/api/admin/llm/call-logs", None),
    ]:
        resp = await client.request(method, url, json=body, headers=member)
        assert resp.status_code == 403, (method, url, resp.status_code)


async def test_settings_get_put_roundtrip(client):
    admin, _ = await _admin_and_member(client)

    resp = await client.get("/api/admin/llm/call-logs/settings", headers=admin)
    assert resp.status_code == 200
    assert resp.json() == {"enabled": False}  # 默认关

    resp = await client.put(
        "/api/admin/llm/call-logs/settings", json={"enabled": True}, headers=admin
    )
    assert resp.status_code == 200
    assert resp.json() == {"enabled": True}
    resp = await client.get("/api/admin/llm/call-logs/settings", headers=admin)
    assert resp.json() == {"enabled": True}

    # 开关打开后 router 立即生效（PUT 已失效开关缓存）
    router = LLMRouter()
    await router.complete("default", [Message(role="user", content="ping")])
    assert len(await _all_logs()) == 1

    resp = await client.put(
        "/api/admin/llm/call-logs/settings", json={"enabled": False}, headers=admin
    )
    assert resp.json() == {"enabled": False}
    await router.complete("default", [Message(role="user", content="pong")])
    assert len(await _all_logs()) == 1  # 关闭后不再记录


async def test_list_pagination_detail_and_clear(client):
    admin, _ = await _admin_and_member(client)
    await _enable_logging()
    router = LLMRouter()
    for i in range(3):
        await router.complete("default", [Message(role="user", content=f"call {i}")])
    await router.complete("relevance", [Message(role="user", content="score this")])

    resp = await client.get("/api/admin/llm/call-logs?limit=2&offset=0", headers=admin)
    assert resp.status_code == 200
    page = resp.json()
    assert page["total"] == 4
    assert len(page["items"]) == 2
    # 时间倒序：最新的（relevance）在最前
    first = page["items"][0]
    assert first["stage"] == "relevance"
    assert first["status"] == "ok"
    assert first["request_preview"].startswith("score this")
    assert first["response_preview"]
    assert "request" not in first  # 列表不含全文

    resp = await client.get("/api/admin/llm/call-logs?limit=2&offset=2", headers=admin)
    assert len(resp.json()["items"]) == 2

    # stage 过滤
    resp = await client.get("/api/admin/llm/call-logs?stage=relevance", headers=admin)
    assert resp.json()["total"] == 1

    # 详情端点给全文
    resp = await client.get(f"/api/admin/llm/call-logs/{first['id']}", headers=admin)
    assert resp.status_code == 200
    detail = resp.json()
    assert detail["request"]["messages"] == [{"role": "user", "content": "score this"}]
    assert detail["response"]

    resp = await client.get(f"/api/admin/llm/call-logs/{uuid.uuid4()}", headers=admin)
    assert resp.status_code == 404

    # 清空
    resp = await client.delete("/api/admin/llm/call-logs", headers=admin)
    assert resp.status_code == 200
    assert resp.json() == {"deleted": 4}
    resp = await client.get("/api/admin/llm/call-logs", headers=admin)
    assert resp.json() == {"total": 0, "items": []}


# ---- 保留策略 ----


async def test_retention_deletes_old_logs(app):
    from datetime import timedelta

    from app.models.base import utcnow

    await _enable_logging()
    async with get_sessionmaker()() as session:
        old = LLMCallLog(
            stage="default",
            provider_name="fake",
            model="fake-default",
            duration_ms=1,
            status="ok",
        )
        old.created_at = utcnow() - timedelta(days=8)
        session.add(old)
        await session.commit()

    router = LLMRouter()
    await router.complete("default", [Message(role="user", content="new call")])
    logs = await _all_logs()
    assert len(logs) == 1  # 8 天前的旧日志已被顺带清理
    assert logs[0].request["messages"][0]["content"] == "new call"
