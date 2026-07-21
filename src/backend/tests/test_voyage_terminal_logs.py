"""任务终端日志持久化：落库 / 回放接口 / 归属校验 / EventBus 咽喉点持久化。"""

import uuid

from tests.conftest import register_and_login


async def _create_project(client, headers, name="proj"):
    resp = await client.post("/api/projects", json={"name": name}, headers=headers)
    assert resp.status_code == 201
    return resp.json()["id"]


async def _create_voyage(client, headers) -> str:
    project_id = await _create_project(client, headers)
    resp = await client.post(
        "/api/voyages",
        json={"kind": "demo", "project_id": project_id, "goal": "目标"},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def test_record_and_fetch_terminal_logs(client, queue_stub):
    from app.services.voyage_logs import record_terminal_log, reset_state

    reset_state()
    token = await register_and_login(client)
    headers = {"Authorization": f"Bearer {token}"}
    voyage_id = await _create_voyage(client, headers)
    rid = uuid.UUID(voyage_id)

    await record_terminal_log(rid, "log", message="第一步开始", level="step")
    await record_terminal_log(rid, "llm", message="大模型输出全文", stage="navigator")
    await record_terminal_log(rid, "log", message="完成", level="success")

    resp = await client.get(f"/api/voyages/{voyage_id}/logs", headers=headers)
    assert resp.status_code == 200, resp.text
    rows = resp.json()
    assert [r["message"] for r in rows] == ["第一步开始", "大模型输出全文", "完成"]
    assert rows[0]["event"] == "log" and rows[0]["level"] == "step"
    assert rows[1]["event"] == "llm" and rows[1]["stage"] == "navigator"
    # id 升序即时间序，前端据此排序
    assert rows[0]["id"] < rows[1]["id"] < rows[2]["id"]


async def test_terminal_logs_owner_only(client, queue_stub):
    from app.services.voyage_logs import record_terminal_log, reset_state

    reset_state()
    token_a = await register_and_login(client, email="owner@example.com")
    headers_a = {"Authorization": f"Bearer {token_a}"}
    voyage_id = await _create_voyage(client, headers_a)
    await record_terminal_log(uuid.UUID(voyage_id), "log", message="私密", level="info")

    token_b = await register_and_login(client, email="stranger@example.com")
    headers_b = {"Authorization": f"Bearer {token_b}"}
    resp = await client.get(f"/api/voyages/{voyage_id}/logs", headers=headers_b)
    assert resp.status_code == 404

    resp = await client.get(f"/api/voyages/{voyage_id}/logs")
    assert resp.status_code == 401


async def test_event_bus_persists_log_not_delta(client, queue_stub, fake_redis):
    """真实 EventBus 在咽喉点持久化 log 事件；高频 llm_delta / llm_start 不落库。"""
    from app.core.events import EventBus
    from app.services.voyage_logs import reset_state

    reset_state()
    token = await register_and_login(client)
    headers = {"Authorization": f"Bearer {token}"}
    voyage_id = await _create_voyage(client, headers)

    bus = EventBus(fake_redis)
    await bus.publish_voyage_event(voyage_id, "log", {"message": "hello", "level": "info"})
    await bus.publish_voyage_event(voyage_id, "llm_start", {"stage": "navigator"})
    await bus.publish_voyage_event(voyage_id, "llm_delta", {"stage": "navigator", "delta": "abc"})
    await bus.publish_voyage_event(voyage_id, "status", {"status": "executing", "cursor": 0})

    resp = await client.get(f"/api/voyages/{voyage_id}/logs", headers=headers)
    assert resp.status_code == 200, resp.text
    rows = resp.json()
    # 只有 log 事件落库；delta/start/status 不落库
    assert [r["message"] for r in rows] == ["hello"]
    assert rows[0]["event"] == "log"


async def test_record_terminal_log_truncates(client, queue_stub):
    from app.services import voyage_logs
    from app.services.voyage_logs import record_terminal_log, reset_state

    reset_state()
    token = await register_and_login(client)
    headers = {"Authorization": f"Bearer {token}"}
    voyage_id = await _create_voyage(client, headers)

    huge = "x" * (voyage_logs.MESSAGE_MAX_CHARS + 5000)
    await record_terminal_log(uuid.UUID(voyage_id), "llm", message=huge, stage="writing")

    resp = await client.get(f"/api/voyages/{voyage_id}/logs", headers=headers)
    rows = resp.json()
    assert len(rows) == 1
    assert rows[0]["message"].startswith("x" * 100)
    assert "truncated" in rows[0]["message"]
    assert len(rows[0]["message"]) < len(huge)
