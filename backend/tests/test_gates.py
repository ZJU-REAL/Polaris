"""闸门 API 测试：可见性 / 审批联动 / 通知发布。"""

import uuid

from app.core.db import get_sessionmaker
from app.schemas.gate import GateCreate
from app.services import gates as gates_service
from tests.conftest import register_and_login


async def _setup(client):
    token = await register_and_login(client)
    headers = {"Authorization": f"Bearer {token}"}
    resp = await client.post("/api/projects", json={"name": "gate-proj"}, headers=headers)
    return headers, resp.json()["id"]


async def _seed_gate(project_id: str, payload: dict | None = None) -> str:
    async with get_sessionmaker()() as session:
        gate = await gates_service.create_gate(
            session,
            GateCreate(
                project_id=uuid.UUID(project_id),
                kind="compute_budget",
                payload=payload,
                requested_by="voyage:demo",
            ),
        )
        return str(gate.id)


async def test_list_gates_membership_and_status_filter(client):
    headers, project_id = await _setup(client)
    gate_id = await _seed_gate(project_id)

    resp = await client.get("/api/gates", headers=headers)
    assert [g["id"] for g in resp.json()] == [gate_id]
    resp = await client.get("/api/gates?status=decided", headers=headers)
    assert resp.json() == []

    # 非成员看不到
    token_b = await register_and_login(client, email="outsider@example.com")
    resp = await client.get("/api/gates", headers={"Authorization": f"Bearer {token_b}"})
    assert resp.json() == []


async def test_approve_enqueues_resume_and_notifies(client, queue_stub, bus_recorder):
    headers, project_id = await _setup(client)
    voyage_id = str(uuid.uuid4())
    gate_id = await _seed_gate(project_id, payload={"voyage_id": voyage_id})

    resp = await client.post(
        f"/api/gates/{gate_id}/approve", json={"comment": "批"}, headers=headers
    )
    assert resp.status_code == 200, resp.text
    gate = resp.json()
    assert gate["status"] == "approved"
    assert gate["comment"] == "批"
    assert gate["decided_by"] is not None
    assert gate["decided_at"] is not None

    assert ("resume_voyage", (voyage_id,), {}) in queue_stub.jobs
    decided = [m for _, m in bus_recorder.notify if m["type"] == "gate.decided"]
    assert decided and decided[0]["gate"]["id"] == gate_id

    # 重复审批 → 409
    resp = await client.post(f"/api/gates/{gate_id}/approve", headers=headers)
    assert resp.status_code == 409

    # decided 过滤能查到
    resp = await client.get("/api/gates?status=decided", headers=headers)
    assert [g["id"] for g in resp.json()] == [gate_id]


async def test_decide_requires_membership(client, queue_stub, bus_recorder):
    headers, project_id = await _setup(client)
    gate_id = await _seed_gate(project_id)

    token_b = await register_and_login(client, email="stranger@example.com")
    resp = await client.post(
        f"/api/gates/{gate_id}/approve", headers={"Authorization": f"Bearer {token_b}"}
    )
    assert resp.status_code == 404

    # 无 body 也可审批（comment 可选）
    resp = await client.post(f"/api/gates/{gate_id}/reject", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["status"] == "rejected"
    assert resp.json()["comment"] is None
