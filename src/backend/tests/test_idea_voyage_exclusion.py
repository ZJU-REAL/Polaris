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
