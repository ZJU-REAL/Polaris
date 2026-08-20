from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.core.db import get_sessionmaker
from app.models.user import User
from app.models.voyage import VoyageMessage, VoyageRun
from app.services import experiments as experiments_service
from app.services import managed_command_watchdog as watchdog
from app.services.managed_ssh import ManagedGPUUsage, ManagedStopResult
from tests.conftest import register_and_login


def _handle() -> dict:
    return {
        "operation_id": "experiment-run",
        "attempt_id": "11111111-1111-1111-1111-111111111111",
        "process_id": 123,
        "process_group_id": 123,
        "context": {
            "phase": "application.run",
            "operation": "experiment-run",
            "display_command": "bash run.sh",
            "repair_scope": "application_files",
        },
    }


async def _open_stale_ask(user_id, *, minutes_old: int = 180):
    async with get_sessionmaker()() as session:
        run = VoyageRun(
            kind="experiment",
            mode="loop",
            goal="test",
            status="paused_ask",
            created_by=user_id,
        )
        session.add(run)
        await session.flush()
        ask = VoyageMessage(
            run_id=run.id,
            seq=1,
            role="agent",
            kind="ask",
            text="keep waiting?",
            payload={
                "ask_kind": "managed_command",
                "context": {"handle": _handle(), "remote_operation_continues": True},
            },
            status="open",
            created_at=datetime.now(UTC) - timedelta(minutes=minutes_old),
        )
        session.add(ask)
        await session.commit()
        return ask.id, run.id


async def test_admin_timeout_caps_the_user_preference(client):
    admin_token = await register_and_login(client)
    headers = {"Authorization": f"Bearer {admin_token}"}

    response = await client.put(
        "/api/admin/settings/managed-command-watchdog",
        json={"max_unanswered_minutes": 60},
        headers=headers,
    )
    assert response.status_code == 200
    response = await client.put(
        "/api/users/me/managed-command-watchdog",
        json={"unanswered_minutes": 240},
        headers=headers,
    )
    assert response.status_code == 200
    assert response.json() == {
        "unanswered_minutes": 240,
        "admin_max_unanswered_minutes": 60,
        "effective_unanswered_minutes": 60,
    }


async def test_stale_gpu_using_command_is_stopped_and_ask_is_updated(client, monkeypatch):
    await register_and_login(client)
    async with get_sessionmaker()() as session:
        user_id = (await session.execute(select(User.id))).scalar_one()
    ask_id, run_id = await _open_stale_ask(user_id)
    stopped: list[str] = []

    async def gpu_usage(*args, **kwargs):
        return ManagedGPUUsage(
            status="active",
            process_alive=True,
            process_ids=(321,),
            used_memory_mib=6144,
        )

    async def stop(*args, **kwargs):
        session = args[0]
        ask = await session.get(VoyageMessage, ask_id)
        # The destructive side effect is unreachable until this watchdog owns
        # the same durable claim used by the answer endpoint.
        assert ask.status == "stopping"
        stopped.append(str(args[1]))
        return ManagedStopResult(status="stopped", confirmed=True)

    monkeypatch.setattr(experiments_service, "managed_command_gpu_usage_by_voyage", gpu_usage)
    monkeypatch.setattr(experiments_service, "stop_managed_command_by_voyage", stop)

    async with get_sessionmaker()() as session:
        events = await watchdog.check_unanswered_managed_commands(session)
        ask = await session.get(VoyageMessage, ask_id)

    assert stopped == [str(run_id)]
    assert len(events) == 1
    assert events[0].action == "stopped_gpu_active"
    assert "6144 MiB" in ask.text
    assert ask.payload["context"]["remote_operation_continues"] is False
    assert [option["id"] for option in ask.payload["options"]] == ["retry", "abort"]
    assert ask.status == "open"


async def test_watchdog_claim_blocks_a_concurrent_keep_running_answer(client, monkeypatch):
    await register_and_login(client)
    async with get_sessionmaker()() as session:
        user_id = (await session.execute(select(User.id))).scalar_one()
    ask_id, _ = await _open_stale_ask(user_id)

    async def gpu_usage(*args, **kwargs):
        return ManagedGPUUsage(
            status="active",
            process_alive=True,
            process_ids=(321,),
            used_memory_mib=2048,
        )

    async def stop(_session, *args, **kwargs):
        # Use a separate session like a concurrent API request.  Once the
        # watchdog has committed open -> stopping, keep-running cannot claim it.
        from sqlalchemy import update

        async with get_sessionmaker()() as other:
            result = await other.execute(
                update(VoyageMessage)
                .where(VoyageMessage.id == ask_id, VoyageMessage.status == "open")
                .values(status="answered")
            )
            await other.commit()
        assert result.rowcount == 0
        return ManagedStopResult(status="stopped", confirmed=True)

    monkeypatch.setattr(experiments_service, "managed_command_gpu_usage_by_voyage", gpu_usage)
    monkeypatch.setattr(experiments_service, "stop_managed_command_by_voyage", stop)

    async with get_sessionmaker()() as session:
        events = await watchdog.check_unanswered_managed_commands(session)

    assert [event.action for event in events] == ["stopped_gpu_active"]


async def test_cancel_during_watchdog_stop_does_not_reopen_the_ask(client, monkeypatch):
    await register_and_login(client)
    async with get_sessionmaker()() as session:
        user_id = (await session.execute(select(User.id))).scalar_one()
    ask_id, _ = await _open_stale_ask(user_id)

    async def gpu_usage(*args, **kwargs):
        return ManagedGPUUsage(
            status="active",
            process_alive=True,
            process_ids=(321,),
            used_memory_mib=1024,
        )

    async def stop(session, voyage_id, *args, **kwargs):
        from app.services import voyages as voyages_service

        run = await session.get(VoyageRun, voyage_id)
        await voyages_service.cancel_voyage(session, run)
        return ManagedStopResult(status="stopped", confirmed=True)

    monkeypatch.setattr(experiments_service, "managed_command_gpu_usage_by_voyage", gpu_usage)
    monkeypatch.setattr(experiments_service, "stop_managed_command_by_voyage", stop)

    async with get_sessionmaker()() as session:
        events = await watchdog.check_unanswered_managed_commands(session)
        ask = await session.get(VoyageMessage, ask_id)

    assert events == []
    assert ask.status == "superseded"


async def test_stale_idle_command_keeps_running(client, monkeypatch):
    await register_and_login(client)
    async with get_sessionmaker()() as session:
        user_id = (await session.execute(select(User.id))).scalar_one()
    ask_id, _ = await _open_stale_ask(user_id)

    async def gpu_usage(*args, **kwargs):
        return ManagedGPUUsage(status="idle", process_alive=True)

    async def unexpected_stop(*args, **kwargs):
        raise AssertionError("idle command must not be stopped")

    monkeypatch.setattr(experiments_service, "managed_command_gpu_usage_by_voyage", gpu_usage)
    monkeypatch.setattr(
        experiments_service, "stop_managed_command_by_voyage", unexpected_stop
    )

    async with get_sessionmaker()() as session:
        events = await watchdog.check_unanswered_managed_commands(session)
        ask = await session.get(VoyageMessage, ask_id)

    assert events == []
    assert ask.payload["context"]["remote_operation_continues"] is True
    assert ask.payload["context"]["unanswered_watchdog"]["status"] == "idle"


async def test_answer_arriving_during_probe_prevents_watchdog_stop(client, monkeypatch):
    await register_and_login(client)
    async with get_sessionmaker()() as session:
        user_id = (await session.execute(select(User.id))).scalar_one()
    ask_id, _ = await _open_stale_ask(user_id)

    async def gpu_usage(session, *args, **kwargs):
        ask = await session.get(VoyageMessage, ask_id)
        ask.status = "answered"
        await session.commit()
        return ManagedGPUUsage(
            status="active",
            process_alive=True,
            process_ids=(321,),
            used_memory_mib=4096,
        )

    async def unexpected_stop(*args, **kwargs):
        raise AssertionError("an answered ask must not trigger the watchdog stop")

    monkeypatch.setattr(experiments_service, "managed_command_gpu_usage_by_voyage", gpu_usage)
    monkeypatch.setattr(
        experiments_service, "stop_managed_command_by_voyage", unexpected_stop
    )

    async with get_sessionmaker()() as session:
        events = await watchdog.check_unanswered_managed_commands(session)

    assert events == []
