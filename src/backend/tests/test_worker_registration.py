"""任务注册守卫（#216）。

入队一个没注册的名字，以前在 ARQ 那边会被**静默丢弃**——库同步就是这么消失了好几天
的。引擎改成进程内执行（#842）后，白名单仍是唯一的事实来源：内联队列按名字去
worker/tasks.py 取函数。所以这里钉死三件事：白名单里的名字都真有其函数、代码里每个
enqueue 的名字都在白名单里、入队未注册的名字当场抛错。
"""

import re
from pathlib import Path

import pytest

from app.core.queue import WORKER_FUNCTIONS, InlineTaskQueue, UnregisteredTaskError

_BACKEND_ROOT = Path(__file__).resolve().parent.parent
_ENQUEUE_CALL = re.compile(r"""\.enqueue\(\s*["']([A-Za-z_][A-Za-z0-9_]*)["']""")


def test_every_whitelisted_task_exists():
    """内联队列按名字去 worker/tasks.py 取函数：白名单里的名字必须真有其函数。"""
    import worker.tasks as worker_tasks

    missing = sorted(n for n in WORKER_FUNCTIONS if not callable(getattr(worker_tasks, n, None)))
    assert not missing, f"白名单里有、worker/tasks.py 里没有：{missing}"


def test_daily_wiki_ingest_is_registered():
    """库同步由每日抓取入队触发，它必须在白名单里（#216 的回归本身）。"""
    assert "daily_wiki_ingest" in WORKER_FUNCTIONS


def test_every_enqueued_task_name_is_registered():
    """扫全仓的 enqueue("...")，任何一个没注册的名字都会被静默丢弃。"""
    unregistered: dict[str, list[str]] = {}
    for root in ("app", "worker"):
        for path in (_BACKEND_ROOT / root).rglob("*.py"):
            for name in _ENQUEUE_CALL.findall(path.read_text(encoding="utf-8")):
                if name not in WORKER_FUNCTIONS:
                    unregistered.setdefault(name, []).append(str(path.relative_to(_BACKEND_ROOT)))
    assert not unregistered, f"入队了未注册的任务：{unregistered}"


async def test_enqueueing_an_unregistered_task_raises():
    """宁可当场炸，也不要像 #216 那样安安静静丢掉。"""
    queue = InlineTaskQueue()
    with pytest.raises(UnregisteredTaskError, match="daily_wiki_ingset"):
        # 故意拼错：这种手滑过去会一路无声无息
        await queue.enqueue("daily_wiki_ingset")


async def test_enqueueing_a_registered_task_passes_the_check():
    """校验只拦未注册的名字，不改变正常入队路径的行为。"""
    from app.core.queue import check_task_name

    for name in WORKER_FUNCTIONS:
        check_task_name(name)  # 不抛即通过


# ---- sqlite 引擎 ----


def test_sqlite_engine_skips_pool_sizing():
    """sqlite 用的是 NullPool/StaticPool，传 pool_size 会直接报错——测试库靠这条保命。"""
    from app.core.db import get_engine

    engine = get_engine()
    assert engine is not None  # 建得起来即说明没把池参数塞给 sqlite


# ---- 启动对账要认领所有在途状态 ----


async def test_reconcile_reclaims_every_in_flight_status(client, monkeypatch):
    """worker 重启把运行掐在 planning / verifying 时，也必须重新入队。

    互斥检查（find_running_ingest_for_library）把**任何非终态**都当成「正在跑」，
    而对账以前只认领 executing。于是被掐在 planning / verifying 的运行永远没人接手，
    它所属的文献库也再发不起新同步——生产上实测三条运行卡了四个半小时，
    而它们的八个兄弟都已完成。
    """
    from app.core.db import get_sessionmaker
    from app.models.voyage import IN_FLIGHT_STATUSES, TERMINAL_STATUSES, VoyageRun
    from worker.tasks import reconcile_stuck_voyages

    parked = ("paused_gate", "paused_error")
    made: dict[str, str] = {}
    async with get_sessionmaker()() as session:
        for status in sorted(IN_FLIGHT_STATUSES | set(parked) | TERMINAL_STATUSES):
            run = VoyageRun(kind="wiki_ingest", mode="pipeline", status=status, goal=status)
            session.add(run)
            await session.flush()
            made[status] = str(run.id)
        await session.commit()

    enqueued: list[str] = []

    class _Redis:
        async def enqueue_job(self, name, run_id, **kwargs):
            enqueued.append(run_id)

    await reconcile_stuck_voyages({"redis": _Redis()})

    for status in IN_FLIGHT_STATUSES:
        assert made[status] in enqueued, f"{status} 没被认领，这条运行会永远卡住"
    for status in (*parked, *TERMINAL_STATUSES):
        assert made[status] not in enqueued, f"{status} 不该被自动重跑"
