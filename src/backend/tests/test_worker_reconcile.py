"""worker 对账：启动认领 + 周期僵死回收（重启/超时/任务丢失时自动恢复在途航程）。"""

import uuid
from datetime import datetime, timedelta

from app.core.db import get_sessionmaker
from app.models.base import utcnow
from app.models.voyage import VoyageRun, VoyageTerminalLog
from tests.conftest import register_and_login
from worker.tasks import reconcile_stale_voyages, reconcile_stuck_voyages


class _RecordingRedis:
    def __init__(self) -> None:
        self.jobs: list[tuple[str, tuple, dict]] = []

    async def enqueue_job(self, func: str, *args, **kwargs):
        self.jobs.append((func, args, kwargs))
        return None


async def _seed_voyage(
    project_id: uuid.UUID, status: str, created_at: datetime | None = None
) -> uuid.UUID:
    async with get_sessionmaker()() as session:
        run = VoyageRun(
            project_id=project_id, kind="experiment", mode="loop", goal="t", status=status
        )
        if created_at is not None:
            run.created_at = created_at
        session.add(run)
        await session.commit()
        return run.id


async def _seed_log(run_id: uuid.UUID, at: datetime) -> None:
    async with get_sessionmaker()() as session:
        session.add(
            VoyageTerminalLog(run_id=run_id, event="log", level="info", message="t", at=at)
        )
        await session.commit()


async def _make_project(client, email: str) -> uuid.UUID:
    token = await register_and_login(client, email)
    headers = {"Authorization": f"Bearer {token}"}
    resp = await client.post("/api/projects", json={"name": "rec-proj"}, headers=headers)
    return uuid.UUID(resp.json()["id"])


async def test_reconcile_enqueues_only_executing(client):
    project_id = await _make_project(client, "reconcile@example.com")

    v_exec = await _seed_voyage(project_id, "executing")
    await _seed_voyage(project_id, "done")
    await _seed_voyage(project_id, "paused_gate")
    await _seed_voyage(project_id, "failed")

    redis = _RecordingRedis()
    await reconcile_stuck_voyages({"redis": redis})

    assert [(f, a[0]) for f, a, _k in redis.jobs] == [("resume_voyage", str(v_exec))]
    # 去重键按时间分桶：固定 id 会被 arq 的结果保留期（1h）静默吞掉入队
    assert redis.jobs[0][2]["_job_id"].startswith(f"reconcile-resume-{v_exec}-")


async def test_stale_reclaim_only_grabs_quiet_voyages(client):
    """周期回收判据：在途 + 终端超时无动静（无日志则看创建时间）才认领。"""
    project_id = await _make_project(client, "reconcile-stale@example.com")
    old = utcnow() - timedelta(hours=2)

    v_quiet = await _seed_voyage(project_id, "executing", created_at=old)  # 无日志且够老
    v_stale_log = await _seed_voyage(project_id, "verifying", created_at=old)
    await _seed_log(v_stale_log, old + timedelta(minutes=5))  # 日志停在两小时前
    v_active = await _seed_voyage(project_id, "verifying", created_at=old)
    await _seed_log(v_active, utcnow())  # 刚有动静
    await _seed_voyage(project_id, "executing")  # 刚创建（可能还在排队）
    await _seed_voyage(project_id, "paused_ask", created_at=old)  # 非 in-flight 绝不碰

    redis = _RecordingRedis()
    await reconcile_stale_voyages({"redis": redis}, stale_minutes=30)

    assert {a[0] for _f, a, _k in redis.jobs} == {str(v_quiet), str(v_stale_log)}
