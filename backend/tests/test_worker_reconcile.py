"""worker 启动对账：认领无人执行的 executing 航程（重启/超时把 ARQ 任务弄丢时自动恢复）。"""

import uuid

from app.core.db import get_sessionmaker
from app.models.voyage import VoyageRun
from tests.conftest import register_and_login
from worker.tasks import reconcile_stuck_voyages


class _RecordingRedis:
    def __init__(self) -> None:
        self.jobs: list[tuple[str, tuple, dict]] = []

    async def enqueue_job(self, func: str, *args, **kwargs):
        self.jobs.append((func, args, kwargs))
        return None


async def _seed_voyage(project_id: uuid.UUID, status: str) -> uuid.UUID:
    async with get_sessionmaker()() as session:
        run = VoyageRun(
            project_id=project_id, kind="experiment", mode="loop", goal="t", status=status
        )
        session.add(run)
        await session.commit()
        return run.id


async def test_reconcile_enqueues_only_executing(client):
    token = await register_and_login(client, "reconcile@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    resp = await client.post("/api/projects", json={"name": "rec-proj"}, headers=headers)
    project_id = uuid.UUID(resp.json()["id"])

    v_exec = await _seed_voyage(project_id, "executing")
    await _seed_voyage(project_id, "done")
    await _seed_voyage(project_id, "paused_gate")
    await _seed_voyage(project_id, "failed")

    redis = _RecordingRedis()
    await reconcile_stuck_voyages({"redis": redis})

    assert [(f, a[0]) for f, a, _k in redis.jobs] == [("resume_voyage", str(v_exec))]
    # _job_id 去重键：同一 voyage 不会重复入队
    assert redis.jobs[0][2]["_job_id"] == f"reconcile-resume-{v_exec}"
