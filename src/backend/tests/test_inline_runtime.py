"""单进程引擎的运行时稳健性（#850）。

- 一条航程同一时刻只有一个驱动者：API 入队与对账落在同一个任务 id 上，对账跳过在途航程；
- 重活不在事件循环上跑（soffice 渲染、导出打包、Zotero 附件读取）；
- 停机先取消并收掉进程内任务，再关数据库；
- 启动时把上一次进程留下的「进行中」行标成失败；
- SQLite 开 WAL + busy_timeout；
- 进程内任务有并发上限。
"""

import asyncio
import threading
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select, text

from app.core.db import get_sessionmaker
from app.core.queue import InlineTaskQueue, _InlineArqRedis, voyage_job_id
from app.models.base import utcnow
from app.models.voyage import VoyageRun


async def _seed_voyage(status: str = "executing", *, old: bool = True) -> uuid.UUID:
    async with get_sessionmaker()() as session:
        run = VoyageRun(kind="experiment", mode="loop", goal="t", status=status)
        if old:
            run.created_at = utcnow() - timedelta(hours=3)
        session.add(run)
        await session.commit()
        return run.id


class _Drivers:
    """替身驱动：记录同时在驱动同一条航程的数量。"""

    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.started: list[tuple[str, str]] = []
        self.active = 0
        self.peak = 0

    def fn(self, name: str):
        async def driver(ctx, run_id):
            self.started.append((name, run_id))
            self.active += 1
            self.peak = max(self.peak, self.active)
            try:
                await self.release.wait()
            finally:
                self.active -= 1

        return driver


@pytest.fixture
def drivers(monkeypatch):
    import worker.tasks as worker_tasks

    d = _Drivers()
    monkeypatch.setattr(worker_tasks, "run_voyage", d.fn("run"))
    monkeypatch.setattr(worker_tasks, "resume_voyage", d.fn("resume"))
    return d


# ---- 1. 一条航程只有一个驱动者 ----


async def test_stale_reconcile_skips_a_voyage_whose_driver_is_running(app, drivers):
    """长实验轮询几小时不写终端日志：对账看日志判它僵死，但驱动者明明还在跑。

    以前对账照样入队 resume（任务 id 与 API 的不同，去重认不出来），同一条航程就有了
    两个驱动者，之后每 15 分钟再多一个。"""
    from worker.tasks import reconcile_stale_voyages

    vid = await _seed_voyage()
    queue = InlineTaskQueue()
    await queue.enqueue("run_voyage", str(vid))  # API 入队：不带 _job_id
    await asyncio.sleep(0)
    assert queue.is_running(voyage_job_id(vid))

    ctx = {"redis": _InlineArqRedis(queue)}
    for _ in range(3):  # 跨多个旧分桶窗口反复对账
        await reconcile_stale_voyages(ctx, stale_minutes=30)
    await asyncio.sleep(0)
    assert drivers.started == [("run", str(vid))]
    assert drivers.peak == 1

    drivers.release.set()
    await queue.drain()
    assert drivers.started == [("run", str(vid))], "对账的 resume 不该在原驱动者结束后补跑"


async def test_stale_reconcile_still_reclaims_an_orphaned_voyage(app, drivers):
    from worker.tasks import reconcile_stale_voyages

    vid = await _seed_voyage()
    queue = InlineTaskQueue()
    await reconcile_stale_voyages({"redis": _InlineArqRedis(queue)}, stale_minutes=30)
    await asyncio.sleep(0)
    assert drivers.started == [("resume", str(vid))]
    drivers.release.set()
    await queue.drain()


async def test_voyage_enqueues_share_one_job_id_whoever_sends_them(drivers):
    """API 入队、对账、每日抓取……不管调用方传什么 id，同一条航程都落在一个 id 上。"""
    queue = InlineTaskQueue()
    vid = str(uuid.uuid4())
    await queue.enqueue("run_voyage", vid)
    await queue.enqueue("run_voyage", vid)
    await queue.enqueue("resume_voyage", vid, _job_id=f"reconcile-resume-{vid}-1", _if_idle=True)
    await queue.enqueue("resume_voyage", vid, _job_id=f"reconcile-resume-{vid}-2", _if_idle=True)
    await asyncio.sleep(0)
    assert drivers.started == [("run", vid)]
    assert list(queue._tasks) == [voyage_job_id(vid)]
    drivers.release.set()
    await queue.drain()
    assert drivers.peak == 1
    assert drivers.started == [("run", vid)]


async def test_user_resume_during_driver_tail_runs_once_afterwards(drivers):
    """用户回答提问 / 批准闸门时，旧驱动者可能还在收尾：resume 不能丢，但也不能并发。"""
    queue = InlineTaskQueue()
    vid = str(uuid.uuid4())
    await queue.enqueue("run_voyage", vid)
    await asyncio.sleep(0)
    for _ in range(3):
        await queue.enqueue("resume_voyage", vid)  # 连点三次：只续跑一次
    drivers.release.set()
    await queue.drain()
    assert drivers.started == [("run", vid), ("resume", vid)]
    assert drivers.peak == 1


# ---- 2. 重活不在事件循环上 ----


async def test_present_build_renders_slides_off_the_event_loop(monkeypatch):
    from app.agents.voyage import actions_present
    from app.agents.voyage.actions import ActionContext
    from tests.test_presentation import _PNG, _RecordingVLM, _wide_deck

    loop_thread = threading.get_ident()
    threads: dict[str, int] = {}

    def fake_render(_pptx):
        threads["render"] = threading.get_ident()
        return [_PNG] * 3

    real_build = actions_present.build_deck

    def spy_build(spec, blobs):
        threads["build"] = threading.get_ident()
        return real_build(spec, blobs)

    async def _no_papers(_ctx):
        return []

    monkeypatch.setattr(actions_present, "soffice_available", lambda: True)
    monkeypatch.setattr(actions_present, "render_slide_images", fake_render)
    monkeypatch.setattr(actions_present, "build_deck", spy_build)
    monkeypatch.setattr(actions_present, "_load_papers", _no_papers)
    vlm = _RecordingVLM()
    vlm.deck = _wide_deck(3)
    run = VoyageRun(id=uuid.uuid4(), kind="presentation", goal="t")
    ctx = ActionContext(run=run, llm=vlm, checkpoint={"present_deck": vlm.deck})
    await actions_present.present_build(ctx, {})
    assert threads["render"] != loop_thread
    assert threads["build"] != loop_thread


async def test_full_export_zips_off_the_event_loop(app, monkeypatch, tmp_path):
    from app.services import full_export

    loop_thread = threading.get_ident()
    seen: list[int] = []
    real_zip = full_export._zip_tree

    def spy_zip(tree, zip_path):
        seen.append(threading.get_ident())
        real_zip(tree, zip_path)

    monkeypatch.setattr(full_export, "_zip_tree", spy_zip)
    async with get_sessionmaker()() as session:
        zip_path, _manifest = await full_export.run_full_export(session, uuid.uuid4(), tmp_path)
    assert zip_path.is_file()
    assert seen and seen[0] != loop_thread
    assert not (tmp_path / "tree").exists()


async def test_zotero_import_reads_files_off_the_event_loop(app, monkeypatch, tmp_path):
    import fakeredis.aioredis

    from app.services import zotero_import

    loop_thread = threading.get_ident()
    seen: dict[str, int] = {}
    real_load = zotero_import._load_bib_entries

    def spy_load(path):
        seen["bib"] = threading.get_ident()
        return real_load(path)

    monkeypatch.setattr(zotero_import, "_load_bib_entries", spy_load)
    bib = tmp_path / "lib.bib"
    bib.write_text("@article{a, title={One}}\n", encoding="utf-8")
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    await zotero_import.run_zotero_import(
        redis,
        task_id="t1",
        bib_path=str(bib),
        zip_path=None,
        library_id=uuid.uuid4(),  # 库不存在：读完 bib 即收尾
        user_id=uuid.uuid4(),
        project_id=None,
    )
    assert seen["bib"] != loop_thread


# ---- 3. 停机 ----


async def test_shutdown_cancels_and_drains_inline_tasks(drivers):
    queue = InlineTaskQueue()
    vid = str(uuid.uuid4())
    await queue.enqueue("run_voyage", vid)
    await queue.enqueue("resume_voyage", vid)  # 记成收尾后的续跑：停机时不该再起
    await asyncio.sleep(0)
    task = queue._tasks[voyage_job_id(vid)]
    await queue.shutdown(grace_s=1.0)
    assert task.cancelled()
    assert queue._tasks == {}
    await queue.enqueue("run_voyage", str(uuid.uuid4()))  # 停机后不再收任务
    assert queue._tasks == {}
    assert drivers.started == [("run", vid)]


async def test_lifespan_stops_background_tasks_before_disposing_the_db(app, monkeypatch):
    """lifespan 退出时：进程内任务先被取消，数据库后关。"""
    import app.core.db as db_mod
    import app.core.queue as queue_mod
    import worker.tasks as worker_tasks
    from app.main import lifespan

    order: list[str] = []
    started = asyncio.Event()

    async def slow(ctx, message="x"):
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            order.append("task-cancelled")
            raise

    real_dispose = db_mod.dispose_engine

    async def spy_dispose():
        order.append("dispose")
        await real_dispose()

    import app.main as main_mod

    monkeypatch.setattr(worker_tasks, "ping_task", slow)
    monkeypatch.setattr(main_mod, "dispose_engine", spy_dispose)
    monkeypatch.setattr("app.core.scheduler.default_jobs", lambda: [])
    async with lifespan(app):
        queue = await queue_mod.get_task_queue()
        await queue.enqueue("ping_task", "x")
        await started.wait()
    assert order == ["task-cancelled", "dispose"]
    assert queue_mod._queue is None


# ---- 4. 启动时收拾上一次进程的半截活 ----


async def test_startup_resets_work_interrupted_by_the_last_shutdown(app, client):
    from app.models.literature_discovery import (
        LiteratureHitTranslation,
        LiteratureOaCache,
        LiteratureSearchHit,
        LiteratureSearchRun,
        LiteratureSourceAttempt,
    )
    from app.models.paper_content import PaperContentVersion
    from app.services.paper_content import create_content_version
    from app.services.startup_recovery import reset_interrupted_work
    from tests.test_literature_discovery_runtime import _create_run
    from tests.test_paper_content import _setup

    running_id, _h, _lib = await _create_run(client, source_config={"sources": ["openalex"]})
    queued_id, _h, _lib = await _create_run(client, source_config={"sources": ["openalex"]})
    _user, _library, _paper, asset = await _setup(client)
    async with get_sessionmaker()() as session:
        running = await session.get(LiteratureSearchRun, running_id)
        running.status = "running"
        session.add(LiteratureSourceAttempt(run_id=running_id, source="arxiv", status="running"))
        hit = LiteratureSearchHit(
            run_id=running_id,
            source="openalex",
            dedup_key=f"doi:10.1/{uuid.uuid4().hex}",
            title="t",
        )
        session.add(hit)
        await session.flush()
        for lang, status in (("zh", "running"), ("ja", "queued"), ("fr", "ready")):
            session.add(
                LiteratureHitTranslation(
                    hit_id=hit.id,
                    target_language=lang,
                    source_hash="h",
                    model_version="m",
                    status=status,
                )
            )
        session.add(LiteratureOaCache(hit_id=hit.id, status="downloading"))
        parsing = await create_content_version(session, asset=asset)
        parsing.status = "mineru_polling"
        ready = await create_content_version(session, asset=asset)
        ready.status = "ready"
        ready.document_vector_state = "building"
        ready.chunk_vector_state = "building"
        await session.commit()
        parsing_id, ready_id = parsing.id, ready.id

    async with get_sessionmaker()() as session:
        counts = await reset_interrupted_work(session)
    assert counts["literature_search_runs"] == 1

    async with get_sessionmaker()() as session:
        run = await session.get(LiteratureSearchRun, running_id)
        assert (run.status, run.error_summary) == ("failed", "INTERRUPTED")
        # queued 的不动：手动运行可以排着等用户点开始
        assert (await session.get(LiteratureSearchRun, queued_id)).status == "queued"
        attempts = (
            await session.scalars(
                select(LiteratureSourceAttempt.status).where(
                    LiteratureSourceAttempt.run_id == running_id
                )
            )
        ).all()
        assert attempts and set(attempts) == {"failed"}
        queued_attempts = (
            await session.scalars(
                select(LiteratureSourceAttempt.status).where(
                    LiteratureSourceAttempt.run_id == queued_id
                )
            )
        ).all()
        assert set(queued_attempts) == {"pending"}
        statuses = dict(
            (
                await session.execute(
                    select(
                        LiteratureHitTranslation.target_language, LiteratureHitTranslation.status
                    )
                )
            ).all()
        )
        assert statuses == {"zh": "failed", "ja": "failed", "fr": "ready"}
        cache = await session.scalar(select(LiteratureOaCache))
        assert cache.status == "failed"
        parsing = await session.get(PaperContentVersion, parsing_id)
        assert (parsing.status, parsing.error_code) == ("failed", "INTERRUPTED")
        ready = await session.get(PaperContentVersion, ready_id)
        assert ready.status == "ready"
        assert (ready.document_vector_state, ready.chunk_vector_state) == ("failed", "failed")


async def test_interrupted_scheduled_discovery_no_longer_blocks_its_schedule(app, client):
    """被杀掉的定时发现停在 running，会让那个库的定时任务永远报 RUN_ALREADY_ACTIVE。"""
    from app.models.literature_discovery import LiteratureSearchRun
    from app.services.startup_recovery import reset_interrupted_work
    from tests.test_literature_discovery_runtime import _create_run

    run_id, _h, _lib = await _create_run(client, source_config={"sources": ["openalex"]})
    async with get_sessionmaker()() as session:
        run = await session.get(LiteratureSearchRun, run_id)
        run.status, run.trigger = "running", "scheduled"
        await session.commit()
        await reset_interrupted_work(session)
    async with get_sessionmaker()() as session:
        active = await session.scalar(
            select(LiteratureSearchRun.id).where(
                LiteratureSearchRun.trigger == "scheduled",
                LiteratureSearchRun.status.in_(("queued", "running")),
            )
        )
    assert active is None


def test_startup_clears_leftover_staging_dirs(tmp_path):
    from app.services.startup_recovery import _clear_staging_dirs

    (tmp_path / "zotero_imports" / "task-a").mkdir(parents=True)
    (tmp_path / "exports" / "task-b" / "tree").mkdir(parents=True)
    (tmp_path / "exports" / "task-c.zip").write_bytes(b"zip")
    assert _clear_staging_dirs(tmp_path) == 2
    assert not (tmp_path / "zotero_imports" / "task-a").exists()
    assert not (tmp_path / "exports" / "task-b").exists()
    assert (tmp_path / "exports" / "task-c.zip").is_file()  # 成品留着等下载


# ---- 5. SQLite 设置 ----


async def test_sqlite_runs_in_wal_mode_with_busy_timeout(app):
    from app.core.config import get_settings

    async with get_sessionmaker()() as session:
        mode = (await session.execute(text("PRAGMA journal_mode"))).scalar_one()
        busy = (await session.execute(text("PRAGMA busy_timeout"))).scalar_one()
        sync = (await session.execute(text("PRAGMA synchronous"))).scalar_one()
        fks = (await session.execute(text("PRAGMA foreign_keys"))).scalar_one()
    assert str(mode).lower() == "wal"
    assert int(busy) == get_settings().sqlite_busy_timeout_ms
    assert int(sync) == 1  # NORMAL
    assert int(fks) == 1


# ---- 6. 并发上限 ----


async def test_inline_queue_caps_concurrency_and_queued_tasks_stay_deduped(monkeypatch):
    import worker.tasks as worker_tasks

    release = asyncio.Event()
    active = 0
    peak = 0
    ran: list[str] = []

    async def heavy(ctx, message="x"):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        ran.append(message)
        try:
            await release.wait()
        finally:
            active -= 1

    monkeypatch.setattr(worker_tasks, "ping_task", heavy)
    queue = InlineTaskQueue(max_jobs=2, max_voyages=1)
    for i in range(5):
        await queue.enqueue("ping_task", str(i), _job_id=f"job-{i}")
    await queue.enqueue("ping_task", "dup", _job_id="job-4")  # 在排队等名额：照样去重
    await asyncio.sleep(0.05)
    assert peak == 2
    assert queue.is_running("job-4")
    release.set()
    await queue.drain()
    assert peak == 2
    assert sorted(ran) == ["0", "1", "2", "3", "4"]


async def test_tasks_waiting_for_a_slot_are_cancelled_at_shutdown(monkeypatch):
    import worker.tasks as worker_tasks

    ran: list[str] = []

    async def heavy(ctx, message="x"):
        ran.append(message)
        await asyncio.Event().wait()

    monkeypatch.setattr(worker_tasks, "ping_task", heavy)
    queue = InlineTaskQueue(max_jobs=1)
    await queue.enqueue("ping_task", "a")
    await queue.enqueue("ping_task", "b")
    await asyncio.sleep(0.02)
    assert ran == ["a"]
    await queue.shutdown(grace_s=1.0)
    assert ran == ["a"]
    assert queue._tasks == {}


async def test_voyages_have_their_own_pool(drivers, monkeypatch):
    """几条长实验占满航程名额时，短任务照样能跑。"""
    import worker.tasks as worker_tasks

    done = asyncio.Event()

    async def short(ctx, message="x"):
        done.set()

    monkeypatch.setattr(worker_tasks, "ping_task", short)
    queue = InlineTaskQueue(max_voyages=1, max_jobs=1)
    await queue.enqueue("run_voyage", str(uuid.uuid4()))
    await queue.enqueue("run_voyage", str(uuid.uuid4()))
    await queue.enqueue("ping_task", "x")
    await asyncio.wait_for(done.wait(), timeout=1)
    assert drivers.peak == 1
    drivers.release.set()
    await queue.drain()


# ---- 7. 旁路写入不和调用方的写锁互等 ----


async def test_side_write_does_not_stall_a_caller_holding_the_write_lock(app):
    """调用方握着没提交的写事务去调 LLM，记账在独立 session 里写：以前两边互等到
    busy_timeout，记账行丢掉、调用方白卡一整个超时。现在立刻返回，提交后补写。"""
    import time

    from app.core.config import get_settings
    from app.core.db import drain_side_writes, side_write
    from app.models.llm_config import LLMUsage
    from app.models.system_setting import SystemSetting

    async def _write(session):
        session.add(LLMUsage(stage="probe", model="m", prompt_tokens=1, completion_tokens=1))

    async with get_sessionmaker()() as holder:
        holder.add(SystemSetting(key="lock-probe", value=1))
        await holder.flush()  # 拿到写锁不提交
        started = time.monotonic()
        await side_write(_write, what="probe")
        assert time.monotonic() - started < 3
        await holder.commit()
    await drain_side_writes(10)
    async with get_sessionmaker()() as session:
        rows = (await session.scalars(select(LLMUsage).where(LLMUsage.stage == "probe"))).all()
        busy = (await session.execute(text("PRAGMA busy_timeout"))).scalar_one()
    assert len(rows) == 1
    # 短超时只给那一次尝试：连接还回池子前已调回
    assert int(busy) == get_settings().sqlite_busy_timeout_ms
