"""同课题 idea 类任务互斥：只有在跑的才挡路，停着等人的不挡；恢复时再查一次。"""

import uuid

import pytest

from app.core.db import get_sessionmaker
from app.models.voyage import VoyageRun
from app.services import voyage_messages as messages_service
from tests.conftest import register_and_login

KNOBS = {"num_ideas": 2, "dedup_threshold": 0.9, "max_context_papers": 20}


async def _setup_project(client, email: str) -> tuple[str, dict]:
    token = await register_and_login(client, email)
    headers = {"Authorization": f"Bearer {token}"}
    resp = await client.post(
        "/api/projects", json={"name": "exclusion", "statement": "s"}, headers=headers
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"], headers


async def _start_forge(client, project_id: str, headers: dict):
    return await client.post(
        f"/api/projects/{project_id}/forge", json={"knobs": KNOBS}, headers=headers
    )


async def _set_status(run_id: str, status: str) -> None:
    async with get_sessionmaker()() as session:
        run = await session.get(VoyageRun, uuid.UUID(run_id))
        run.status = status
        await session.commit()


async def _status(run_id: str) -> str:
    async with get_sessionmaker()() as session:
        run = await session.get(VoyageRun, uuid.UUID(run_id))
        return run.status


@pytest.mark.parametrize("parked", ["paused_error", "paused_ask", "paused_gate"])
async def test_parked_idea_run_does_not_block_new_idea_work(client, queue_stub, parked):
    project_id, headers = await _setup_project(client, f"excl-{parked}@example.com")
    first = await _start_forge(client, project_id, headers)
    assert first.status_code == 201, first.text
    await _set_status(first.json()["id"], parked)

    # 以前：任何没结束的 idea 任务都算「在跑」，这里是 409，直到用户去取消那条停着的
    second = await _start_forge(client, project_id, headers)
    assert second.status_code == 201, second.text
    state = (await client.get(f"/api/projects/{project_id}/forge/state", headers=headers)).json()
    assert state["running_voyage_id"] == second.json()["id"]


@pytest.mark.parametrize("in_flight", ["planning", "executing", "verifying", "replanning"])
async def test_in_flight_idea_run_still_blocks(client, queue_stub, in_flight):
    project_id, headers = await _setup_project(client, f"excl-{in_flight}@example.com")
    first = await _start_forge(client, project_id, headers)
    await _set_status(first.json()["id"], in_flight)

    for path, body in (
        ("forge", {"knobs": KNOBS}),
        ("review/tournament", {}),
    ):
        resp = await client.post(f"/api/projects/{project_id}/{path}", json=body, headers=headers)
        assert resp.status_code == 409, resp.text
        assert resp.json()["detail"] == "IDEA_VOYAGE_ALREADY_RUNNING"


async def test_resuming_a_parked_idea_run_while_another_runs_is_refused(client, queue_stub):
    project_id, headers = await _setup_project(client, "excl-resume@example.com")
    parked_id = (await _start_forge(client, project_id, headers)).json()["id"]
    await _set_status(parked_id, "paused_error")
    running_id = (await _start_forge(client, project_id, headers)).json()["id"]
    jobs_before = list(queue_stub.jobs)

    resp = await client.post(f"/api/voyages/{parked_id}/resume", headers=headers)
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"] == "IDEA_VOYAGE_ALREADY_RUNNING"
    # 原样停着，没有入队
    assert await _status(parked_id) == "paused_error"
    assert queue_stub.jobs == jobs_before

    # 那条跑完了，就能恢复
    await _set_status(running_id, "done")
    resp = await client.post(f"/api/voyages/{parked_id}/resume", headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "executing"
    assert ("resume_voyage", (parked_id,), {}) in queue_stub.jobs


async def test_answering_a_parked_idea_run_while_another_runs_is_refused(client, queue_stub):
    project_id, headers = await _setup_project(client, "excl-answer@example.com")
    parked_id = (await _start_forge(client, project_id, headers)).json()["id"]
    async with get_sessionmaker()() as session:
        ask = await messages_service.append_message(
            session,
            uuid.UUID(parked_id),
            role="agent",
            kind="ask",
            text="怎么处理？",
            payload={"ask_kind": "fatal_step"},
            status="open",
        )
        ask_id = str(ask.id)
    await _set_status(parked_id, "paused_ask")
    running_id = (await _start_forge(client, project_id, headers)).json()["id"]
    jobs_before = list(queue_stub.jobs)
    url = f"/api/voyages/{parked_id}/asks/{ask_id}/answer"

    resp = await client.post(url, json={"choice": "retry"}, headers=headers)
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"] == "IDEA_VOYAGE_ALREADY_RUNNING"
    assert await _status(parked_id) == "paused_ask"
    assert queue_stub.jobs == jobs_before
    # 提问还开着，之后照样能答
    detail = (await client.get(f"/api/voyages/{parked_id}", headers=headers)).json()
    assert detail["open_ask"]["id"] == ask_id

    await _set_status(running_id, "done")
    resp = await client.post(url, json={"choice": "retry"}, headers=headers)
    assert resp.status_code == 201, resp.text
    assert await _status(parked_id) == "executing"


async def test_giving_up_a_parked_idea_run_is_always_allowed(client, queue_stub):
    project_id, headers = await _setup_project(client, "excl-abort@example.com")
    parked_id = (await _start_forge(client, project_id, headers)).json()["id"]
    async with get_sessionmaker()() as session:
        ask = await messages_service.append_message(
            session,
            uuid.UUID(parked_id),
            role="agent",
            kind="ask",
            text="怎么处理？",
            payload={"ask_kind": "fatal_step"},
            status="open",
        )
        ask_id = str(ask.id)
    await _set_status(parked_id, "paused_ask")
    await _start_forge(client, project_id, headers)

    resp = await client.post(
        f"/api/voyages/{parked_id}/asks/{ask_id}/answer",
        json={"choice": "abort"},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    assert await _status(parked_id) == "failed"


async def test_non_idea_runs_are_not_subject_to_the_check(client, queue_stub):
    project_id, headers = await _setup_project(client, "excl-other@example.com")
    await _start_forge(client, project_id, headers)  # 在跑的 idea 任务
    resp = await client.post(
        "/api/voyages",
        json={"kind": "demo", "project_id": project_id, "goal": "g"},
        headers=headers,
    )
    demo_id = resp.json()["id"]
    await _set_status(demo_id, "paused_error")

    resp = await client.post(f"/api/voyages/{demo_id}/resume", headers=headers)
    assert resp.status_code == 200, resp.text


async def _add_gate(project_id: str, voyage_id: str) -> str:
    from app.models.gate import Gate

    async with get_sessionmaker()() as session:
        gate = Gate(
            project_id=uuid.UUID(project_id),
            kind="idea_goal",
            status="pending",
            payload={"voyage_id": voyage_id},
            requested_by="agent",
        )
        session.add(gate)
        await session.commit()
        return str(gate.id)


async def test_approving_a_gate_on_a_parked_idea_run_while_another_runs_is_refused(
    client, queue_stub
):
    """批准闸门 = 恢复那条任务，和「重试」一样要先查互斥。"""
    project_id, headers = await _setup_project(client, "excl-gate@example.com")
    parked_id = (await _start_forge(client, project_id, headers)).json()["id"]
    await _set_status(parked_id, "paused_gate")
    gate_id = await _add_gate(project_id, parked_id)
    running_id = (await _start_forge(client, project_id, headers)).json()["id"]
    jobs_before = list(queue_stub.jobs)

    # 以前：闸门直接记成批准并入队恢复，同课题就有两条 idea 任务同时在跑
    resp = await client.post(f"/api/gates/{gate_id}/approve", headers=headers)
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"] == "IDEA_VOYAGE_ALREADY_RUNNING"
    assert queue_stub.jobs == jobs_before
    assert await _status(parked_id) == "paused_gate"
    # 闸门还是待审批：那条跑完之后可以再批
    pending = (await client.get("/api/gates?status=pending", headers=headers)).json()
    assert gate_id in [g["id"] for g in pending]

    await _set_status(running_id, "done")
    resp = await client.post(f"/api/gates/{gate_id}/approve", headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "approved"
    assert ("resume_voyage", (parked_id,), {}) in queue_stub.jobs


async def test_rejecting_a_gate_on_a_parked_idea_run_is_always_allowed(client, queue_stub):
    project_id, headers = await _setup_project(client, "excl-gate-reject@example.com")
    parked_id = (await _start_forge(client, project_id, headers)).json()["id"]
    await _set_status(parked_id, "paused_gate")
    gate_id = await _add_gate(project_id, parked_id)
    await _start_forge(client, project_id, headers)

    resp = await client.post(f"/api/gates/{gate_id}/reject", headers=headers)
    assert resp.status_code == 200, resp.text
    assert await _status(parked_id) == "failed"


async def test_forge_state_reports_a_parked_idea_run(client, queue_stub):
    """停着的任务不算「进行中」，但想法页还得知道它在等人处理。"""
    project_id, headers = await _setup_project(client, "excl-parked-state@example.com")
    state = (await client.get(f"/api/projects/{project_id}/forge/state", headers=headers)).json()
    assert state["parked_run"] is None

    parked_id = (await _start_forge(client, project_id, headers)).json()["id"]
    async with get_sessionmaker()() as session:
        run = await session.get(VoyageRun, uuid.UUID(parked_id))
        run.status = "paused_error"
        run.checkpoint = {
            **(run.checkpoint or {}),
            "last_error": {
                "code": "NETWORK",
                "message": "执行步骤时出错：网络连接失败",
                "detail": "ConnectError: boom",
                "transient": True,
                "source": "action",
            },
            "auto_retry": {"attempt": 2, "next_at": "2099-01-01T00:00:00+00:00"},
        }
        await session.commit()

    state = (await client.get(f"/api/projects/{project_id}/forge/state", headers=headers)).json()
    # 以前：running_voyage_id 为空、也没有别的字段提到它，页面上什么都看不出来
    assert state["running_voyage_id"] is None
    parked = state["parked_run"]
    assert parked["voyage_id"] == parked_id
    assert parked["kind"] == "idea_forge"
    assert parked["status"] == "paused_error"
    assert parked["error_code"] == "NETWORK"
    assert parked["error_detail"] == "ConnectError: boom"
    assert parked["retry_attempt"] == 2
    assert parked["retry_at"].startswith("2099-01-01")

    # 另起一条在跑的：两个都报，互不遮盖
    running_id = (await _start_forge(client, project_id, headers)).json()["id"]
    state = (await client.get(f"/api/projects/{project_id}/forge/state", headers=headers)).json()
    assert state["running_voyage_id"] == running_id
    assert state["parked_run"]["voyage_id"] == parked_id

    # 取消之后不再报
    resp = await client.post(f"/api/voyages/{parked_id}/cancel", headers=headers)
    assert resp.status_code == 200, resp.text
    state = (await client.get(f"/api/projects/{project_id}/forge/state", headers=headers)).json()
    assert state["parked_run"] is None
