"""任务注册守卫（#216）。

arq 只执行 ``WorkerSettings.functions`` 里注册过的函数，入队一个没注册的名字
会被**静默丢弃**：入队方拿不到任何错误，worker 也只在执行时打一行
``function 'x' not found``。库同步就是这么消失了好几天的——改成事件驱动后
``daily_wiki_ingest`` 从 cron 摘掉却没进 functions，而抓取步骤照报成功。

所以这里钉死三件事：注册表与白名单一致、代码里每个 enqueue 的名字都在白名单里、
入队未注册的名字当场抛错。
"""

import re
from pathlib import Path

import pytest

from app.core.queue import WORKER_FUNCTIONS, ArqTaskQueue, UnregisteredTaskError
from worker.settings import registered_function_names

_BACKEND_ROOT = Path(__file__).resolve().parent.parent
_ENQUEUE_CALL = re.compile(r"""\.enqueue\(\s*["']([A-Za-z_][A-Za-z0-9_]*)["']""")


def test_worker_registration_matches_the_enqueue_whitelist():
    """两边必须一字不差，否则入队会被丢。"""
    assert registered_function_names() == set(WORKER_FUNCTIONS)


def test_daily_wiki_ingest_is_registered():
    """库同步由每日抓取入队触发，它必须在 functions 里（#216 的回归本身）。"""
    assert "daily_wiki_ingest" in registered_function_names()


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
    queue = ArqTaskQueue()
    with pytest.raises(UnregisteredTaskError, match="daily_wiki_ingset"):
        # 故意拼错：这种手滑过去会一路无声无息
        await queue.enqueue("daily_wiki_ingset")


async def test_enqueueing_a_registered_task_passes_the_check():
    """校验只拦未注册的名字，不改变正常入队路径的行为。"""
    from app.core.queue import check_task_name

    for name in WORKER_FUNCTIONS:
        check_task_name(name)  # 不抛即通过


# ---- 连接池要装得下 worker 的并发 ----


def test_db_pool_fits_worker_concurrency():
    """池子上限必须 ≥ worker 并发跑打分时的连接需求。

    生产上曾经不满足：SQLAlchemy 默认 5+10=15，而 max_jobs(10) 个 voyage 各开
    _LLM_CONCURRENCY(5) 个协程、每篇论文一个 session ⇒ 峰值 50。50 抢 15 的结果是
    大批协程等满 pool_timeout 后超时，该篇打分失败、状态停在 candidate——生产库里
    积了 2400 多条从没打过分的候选就是这么来的。
    """
    from app.agents.voyage.actions_wiki import _LLM_CONCURRENCY
    from app.core.config import get_settings
    from worker.settings import WorkerSettings

    max_jobs = getattr(WorkerSettings, "max_jobs", 10)
    settings = get_settings()
    ceiling = settings.db_pool_size + settings.db_max_overflow
    # 每个 voyage：打分并发 + 引擎自己记步骤/检查点的那条
    need = max_jobs * (_LLM_CONCURRENCY + 1)
    assert ceiling >= need, (
        f"连接池上限 {ceiling} < 峰值需求 {need}"
        f"（max_jobs={max_jobs} × (打分并发 {_LLM_CONCURRENCY} + 1)）"
    )


def test_sqlite_engine_skips_pool_sizing():
    """sqlite 用的是 NullPool/StaticPool，传 pool_size 会直接报错——测试库靠这条保命。"""
    from app.core.db import get_engine

    engine = get_engine()
    assert engine is not None  # 建得起来即说明没把池参数塞给 sqlite
