"""Real engine/SQLite ownership regressions; actions are controlled local coroutines."""

import asyncio
import uuid
from datetime import UTC, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select, update

from app.agents.voyage.engine import VoyageEngine
from app.core import voyage_lease
from app.core.db import get_sessionmaker
from app.core.llm.router import LLMRouter
from app.core.voyage_lease import VoyageLeaseLost, claim_execution, fence_execution
from app.models.base import utcnow
from app.models.voyage import VoyageRun, VoyageStep
from tests.conftest import RecordingBus


async def seed_run(*, status="planning", expired=False, running_step=False):
    async with get_sessionmaker()() as session:
        run = VoyageRun(
            kind="demo",
            goal="isolated ownership check",
            status=status,
            plan=[{"title": "one action", "action": "sleep", "params": {"seconds": 0}}],
            checkpoint={"guidance": {}},
            execution_token=uuid.uuid4() if expired else None,
            execution_expires_at=utcnow() - timedelta(seconds=1) if expired else None,
        )
        session.add(run)
        await session.flush()
        if running_step:
            session.add(
                VoyageStep(
                    run_id=run.id,
                    seq=0,
                    rank=0,
                    title="one action",
                    action="sleep",
                    params={"seconds": 0},
                    status="running",
                    attempt=3,
                )
            )
        await session.commit()
        return run.id


def make_engine():
    return VoyageEngine(event_bus=RecordingBus(), llm_router=LLMRouter())


async def state(run_id):
    async with get_sessionmaker()() as session:
        run = await session.get(VoyageRun, run_id)
        steps = list(await session.scalars(select(VoyageStep).where(VoyageStep.run_id == run_id)))
        return run, steps


@pytest.mark.parametrize(
    "first,second",
    [
        ("run", "resume"),
        ("resume", "run"),
        ("run", "run"),
        ("resume", "resume"),
    ],
)
async def test_duplicate_run_resume_never_resets_or_reexecutes_active_step(app, first, second):
    run_id = await seed_run()
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []
    a, b = make_engine(), make_engine()

    async def action(ctx, step_def):
        calls.append(ctx.step_id)
        entered.set()
        await release.wait()
        return {"content": "one result", "self_check": {"passed": True}}

    a.helm.execute = action
    b.helm.execute = action
    owner = asyncio.create_task(getattr(a, first)(run_id))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        await asyncio.wait_for(getattr(b, second)(run_id), 3)
        _, steps = await state(run_id)
        assert len(calls) == 1
        assert steps[0].status == "running"
        assert steps[0].attempt == 1
    finally:
        release.set()
        await asyncio.wait_for(owner, 3)
    run, steps = await state(run_id)
    assert run.status == "done"
    assert run.execution_token is None
    assert len(calls) == 1
    assert len(steps[0].attempts) == 1
    assert steps[0].status == "passed"


async def test_expired_owner_can_be_recovered_and_terminal_delivery_is_noop(app):
    run_id = await seed_run(status="executing", expired=True, running_step=True)
    engine = make_engine()
    calls = []

    async def action(ctx, step_def):
        calls.append(ctx.step_id)
        return {"content": "recovered", "self_check": {"passed": True}}

    engine.helm.execute = action
    await engine.resume(run_id)
    run, steps = await state(run_id)
    assert run.status == "done"
    assert run.execution_token is None and run.execution_expires_at is None
    assert steps[0].attempt == 1
    assert len(calls) == 1
    await engine.run(run_id)
    await engine.resume(run_id)
    assert len(calls) == 1


async def test_expired_token_cannot_publish_and_old_release_cannot_clear_successor(app):
    run_id = await seed_run()
    entered, exit_old = asyncio.Event(), asyncio.Event()

    async def original_owner():
        async with claim_execution(run_id) as acquired:
            assert acquired
            entered.set()
            await exit_old.wait()
            async with get_sessionmaker()() as session:
                with pytest.raises(VoyageLeaseLost):
                    await fence_execution(session, run_id)

    original = asyncio.create_task(original_owner())
    try:
        await asyncio.wait_for(entered.wait(), 3)
        async with get_sessionmaker()() as session:
            await session.execute(
                update(VoyageRun)
                .where(VoyageRun.id == run_id)
                .values(execution_expires_at=utcnow() - timedelta(seconds=1))
            )
            await session.commit()
        async with claim_execution(run_id) as acquired:
            assert acquired
            successor = (await state(run_id))[0].execution_token
            exit_old.set()
            await asyncio.wait_for(original, 3)
            assert (await state(run_id))[0].execution_token == successor
            async with get_sessionmaker()() as session:
                await fence_execution(session, run_id)
                await session.commit()
    finally:
        exit_old.set()
        if not original.done():
            original.cancel()
        await asyncio.gather(original, return_exceptions=True)
    assert (await state(run_id))[0].execution_token is None


async def test_lease_heartbeat_cancels_owner_after_ownership_is_lost(app, monkeypatch):
    monkeypatch.setattr(voyage_lease, "HEARTBEAT_SECONDS", 0.01)
    run_id = await seed_run()
    entered = asyncio.Event()

    async def owner():
        async with claim_execution(run_id) as acquired:
            assert acquired
            entered.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(owner())
    successor = uuid.uuid4()
    try:
        await asyncio.wait_for(entered.wait(), 3)
        async with get_sessionmaker()() as session:
            await session.execute(
                update(VoyageRun)
                .where(VoyageRun.id == run_id)
                .values(
                    execution_token=successor, execution_expires_at=utcnow() + timedelta(minutes=1)
                )
            )
            await session.commit()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 3)
        assert (await state(run_id))[0].execution_token == successor
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("paused_at", ["action", "verification"])
async def test_superseded_engine_cannot_publish_action_or_verification_result(app, paused_at):
    run_id = await seed_run()
    entered, release = asyncio.Event(), asyncio.Event()
    engine = make_engine()

    async def action(ctx, step_def):
        if paused_at == "action":
            entered.set()
            await release.wait()
        ctx.checkpoint["owned_result"] = "stale worker"
        return {"content": "stale result"}

    async def verify(run, step_def, observation):
        if paused_at == "verification":
            entered.set()
            await release.wait()
        return {"passed": True, "reason": "stale verdict"}, {"prompt_tokens": 10}

    engine.helm.execute = action
    engine.sextant.verify = verify
    task = asyncio.create_task(engine.run(run_id))
    successor = uuid.uuid4()
    try:
        await asyncio.wait_for(entered.wait(), 3)
        async with get_sessionmaker()() as session:
            await session.execute(
                update(VoyageRun)
                .where(VoyageRun.id == run_id)
                .values(
                    execution_token=successor, execution_expires_at=utcnow() + timedelta(minutes=1)
                )
            )
            await session.commit()
        release.set()
        with pytest.raises(VoyageLeaseLost):
            await asyncio.wait_for(task, 3)
        run, steps = await state(run_id)
        assert run.execution_token == successor
        assert steps[0].verdict is None
        assert steps[0].status != "passed"
        if paused_at == "action":
            assert steps[0].observation is None
            assert "owned_result" not in (run.checkpoint or {})
        assert not any(
            event == "step" and data["step"]["status"] == "passed"
            for _, event, data in engine._bus.voyage_events
        )
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_expired_current_token_is_rejected_even_before_successor_claims(app):
    run_id = await seed_run()
    async with claim_execution(run_id) as acquired:
        assert acquired
        async with get_sessionmaker()() as session:
            await session.execute(
                update(VoyageRun)
                .where(VoyageRun.id == run_id)
                .values(execution_expires_at=utcnow() - timedelta(seconds=1))
            )
            await session.commit()
        async with get_sessionmaker()() as session:
            # Load the ORM entity too: SQLite returns a naive datetime in its identity map.
            run = await session.get(VoyageRun, run_id)
            assert run.execution_token is not None
            with pytest.raises(VoyageLeaseLost):
                await fence_execution(session, run_id)


async def test_desktop_reset_clears_previous_process_only_and_preserves_current_owner(
    app, monkeypatch
):
    monkeypatch.setattr(voyage_lease, "get_settings", lambda: SimpleNamespace(is_desktop=True))
    previous_id = await seed_run(status="executing")
    current_id = await seed_run(status="executing")
    previous_token = uuid.uuid4()
    async with get_sessionmaker()() as session:
        await session.execute(
            update(VoyageRun)
            .where(VoyageRun.id == previous_id)
            .values(
                execution_token=previous_token, execution_expires_at=utcnow() + timedelta(minutes=5)
            )
        )
        await session.commit()
    async with claim_execution(current_id) as acquired:
        assert acquired
        current_token = voyage_lease.current_execution_token()
        assert current_token is not None
        assert await voyage_lease.reset_desktop_execution_leases() == 1
        assert (await state(previous_id))[0].execution_token is None
        assert (await state(current_id))[0].execution_token == current_token
        # A repeated startup call must not evict work already dispatched by this process.
        assert await voyage_lease.reset_desktop_execution_leases() == 0
        async with get_sessionmaker()() as session:
            await fence_execution(session, current_id)
            await session.commit()
    assert (await state(current_id))[0].execution_token is None


async def test_server_startup_cannot_clear_other_workers_valid_lease(app, monkeypatch):
    monkeypatch.setattr(voyage_lease, "get_settings", lambda: SimpleNamespace(is_desktop=False))
    run_id = await seed_run()
    token = uuid.uuid4()
    async with get_sessionmaker()() as session:
        await session.execute(
            update(VoyageRun)
            .where(VoyageRun.id == run_id)
            .values(execution_token=token, execution_expires_at=utcnow() + timedelta(minutes=5))
        )
        await session.commit()
    with pytest.raises(RuntimeError, match="restricted to Desktop"):
        await voyage_lease.reset_desktop_execution_leases()
    assert (await state(run_id))[0].execution_token == token


async def test_heartbeat_extends_only_live_lease(app, monkeypatch):
    monkeypatch.setattr(voyage_lease, "HEARTBEAT_SECONDS", 0.01)
    monkeypatch.setattr(voyage_lease, "LEASE_SECONDS", 0.2)
    run_id = await seed_run()
    async with claim_execution(run_id) as acquired:
        assert acquired
        before = (await state(run_id))[0].execution_expires_at
        await asyncio.sleep(0.05)
        after = (await state(run_id))[0].execution_expires_at
        if before.tzinfo is None:
            before = before.replace(tzinfo=UTC)
        if after.tzinfo is None:
            after = after.replace(tzinfo=UTC)
        assert after > before
        assert (await state(run_id))[0].execution_token == voyage_lease.current_execution_token()


async def test_expired_lease_heartbeat_cannot_revive_owner(app, monkeypatch):
    monkeypatch.setattr(voyage_lease, "HEARTBEAT_SECONDS", 0.01)
    run_id = await seed_run()
    entered = asyncio.Event()

    async def owner():
        async with claim_execution(run_id) as acquired:
            assert acquired
            entered.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(owner())
    try:
        await asyncio.wait_for(entered.wait(), 1)
        async with get_sessionmaker()() as session:
            await session.execute(
                update(VoyageRun)
                .where(VoyageRun.id == run_id)
                .values(execution_expires_at=utcnow() - timedelta(seconds=1))
            )
            await session.commit()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 1)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert (await state(run_id))[0].execution_token is None


async def test_inline_shutdown_cancels_and_releases_owner_before_returning(app, monkeypatch):
    from app.core.queue import InlineTaskQueue
    from worker import tasks as worker_tasks

    run_id = await seed_run()
    entered = asyncio.Event()

    async def controlled_owner(ctx, run_id):
        async with claim_execution(uuid.UUID(run_id)) as acquired:
            assert acquired
            entered.set()
            await asyncio.Event().wait()

    monkeypatch.setattr(worker_tasks, "run_voyage", controlled_owner)
    queue = InlineTaskQueue()
    await queue.enqueue_job("run_voyage", str(run_id), _job_id="shutdown-lease")
    await asyncio.wait_for(entered.wait(), 1)
    assert (await state(run_id))[0].execution_token is not None
    await queue.cancel_and_drain(timeout=1)
    assert (await state(run_id))[0].execution_token is None
    assert not queue._tasks


@pytest.mark.parametrize("desktop", [True, False])
async def test_lifespan_resets_before_dispatch_and_drains_before_database_close(
    monkeypatch, desktop
):
    from contextlib import asynccontextmanager

    from app import main as main_module
    from app.core import queue as queue_module
    from app.core.queue import InlineTaskQueue
    from worker import tasks as worker_tasks

    events = []
    periodic = asyncio.Event()

    async def record(event):
        events.append(event)

    @asynccontextmanager
    async def fake_session():
        yield object()

    class Queue(InlineTaskQueue):
        async def enqueue(self, func, *args, **kwargs):
            events.append("enqueue:" + func)

        async def cancel_and_drain(self, **kwargs):
            events.append("drain")

    queue = Queue()

    async def get_queue():
        return queue

    async def startup(ctx):
        events.append("startup_reconcile")
        await ctx["redis"].enqueue_job("resume_voyage", "synthetic")

    async def tick(ctx):
        events.append("periodic_reconcile")
        periodic.set()

    monkeypatch.setattr(
        main_module, "get_settings", lambda: SimpleNamespace(is_sqlite=True, is_desktop=desktop)
    )
    monkeypatch.setattr(main_module, "create_all", lambda: record("schema"))
    monkeypatch.setattr(main_module, "get_sessionmaker", lambda: fake_session)
    monkeypatch.setattr(
        main_module, "ensure_guidance_documents", lambda session: record("guidance")
    )
    monkeypatch.setattr(main_module, "load_disciplines", lambda: events.append("disciplines"))
    monkeypatch.setattr(
        main_module,
        "get_crdt_stream_subscriber",
        lambda: SimpleNamespace(start=lambda: events.append("stream")),
    )
    monkeypatch.setattr(main_module, "stop_crdt_stream_subscriber", lambda: record("stop_stream"))
    monkeypatch.setattr(main_module, "reset_crdt_rooms", lambda: record("rooms_closed"))
    monkeypatch.setattr(main_module, "dispose_engine", lambda: record("database_closed"))
    monkeypatch.setattr(main_module, "close_redis", lambda: record("redis_closed"))
    monkeypatch.setattr(main_module, "_DESKTOP_RECONCILE_SECONDS", 0.01)
    monkeypatch.setattr(
        voyage_lease, "reset_desktop_execution_leases", lambda: record("lease_reset")
    )
    monkeypatch.setattr(queue_module, "get_task_queue", get_queue)
    monkeypatch.setattr(worker_tasks, "reconcile_stuck_voyages", startup)
    monkeypatch.setattr(worker_tasks, "reconcile_stale_voyages", tick)
    async with main_module.lifespan(SimpleNamespace()):
        events.append("http_ready")
        if desktop:
            await asyncio.wait_for(periodic.wait(), 1)
    if desktop:
        assert events.index("schema") < events.index("lease_reset")
        assert events.index("lease_reset") < events.index("startup_reconcile")
        assert events.index("startup_reconcile") < events.index("http_ready")
        assert events.index("drain") < events.index("database_closed")
        assert "periodic_reconcile" in events
    else:
        assert "lease_reset" not in events
        assert "startup_reconcile" not in events
        assert "periodic_reconcile" not in events
        assert "drain" not in events


async def test_sqlite_busy_retry_rolls_back_complete_transaction_only():
    from sqlalchemy.exc import OperationalError

    from app.core.sqlite_retry import retry_sqlite_busy

    calls = []

    async def transaction():
        calls.append("attempt")
        if calls.count("attempt") == 1:
            raise OperationalError("UPDATE", {}, Exception("database is locked"))
        return "published"

    async def rollback():
        calls.append("rollback")

    assert await retry_sqlite_busy(transaction, rollback, base_delay=0) == "published"
    assert calls == ["attempt", "rollback", "attempt"]

    async def unrelated_failure():
        raise OperationalError("UPDATE", {}, Exception("disk full"))

    with pytest.raises(OperationalError, match="disk full"):
        await retry_sqlite_busy(unrelated_failure, rollback, base_delay=0)
