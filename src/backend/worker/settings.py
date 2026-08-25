"""ARQ WorkerSettings：``arq worker.settings.WorkerSettings`` 启动。"""

from arq import cron, func
from arq.connections import RedisSettings

from app.core.config import get_settings
from app.core.queue import WORKER_FUNCTIONS
from worker.tasks import (
    daily_feed_sync,
    daily_publication_match,
    daily_wiki_ingest,
    index_papers_fulltext_task,
    match_user_publications,
    ping_task,
    reconcile_stale_voyages,
    reconcile_stuck_voyages,
    resume_voyage,
    run_voyage,
    watch_unanswered_managed_commands,
)

# 航程任务超时：GPU 训练轮合法地跑数小时；1h 的默认会把轮询任务掐死→ARQ 按任务
# 年龄指数延迟重试→voyage 被晾数小时（实测）。轮内预算（budget.max_hours）才是守卫。
VOYAGE_JOB_TIMEOUT_SECONDS = 12 * 3600


class WorkerSettings:
    functions = [
        ping_task,
        func(run_voyage, timeout=VOYAGE_JOB_TIMEOUT_SECONDS),
        func(resume_voyage, timeout=VOYAGE_JOB_TIMEOUT_SECONDS),
        match_user_publications,
        index_papers_fulltext_task,
        daily_feed_sync,
        # 库同步：由「每日论文抓取」跑完后入队触发。它一度只挂在 cron 上，改成
        # 事件驱动后忘了加到这里，于是每次入队都被 arq 丢掉（#216）。
        daily_wiki_ingest,
        daily_publication_match,
    ]
    # 抓取时刻可由管理员配置（SystemSetting daily_feed_sync_time，默认 UTC 02:30 =
    # 北京 10:30；arXiv 约北京 10:00 放新公告）。arq 的 cron 时刻在 worker 启动时就固定
    # 了，改设置得重启才生效——所以这里让 cron 每 15 分钟空转一次，由任务自己判断到点
    # 没有、今天跑过没有。空转一次只是一条查询，代价可以忽略。
    #
    # 库同步不在这里：它由「每日论文抓取」跑完后触发（daily.sync_libraries），池子备好
    # 了才同步，时刻不用猜。发表匹配仍按时刻派生（抓取 + 150 分钟）。
    _CHECKPOINT_MINUTES = {0, 15, 30, 45}
    cron_jobs = [
        cron(daily_feed_sync, minute=_CHECKPOINT_MINUTES),
        cron(daily_publication_match, minute=_CHECKPOINT_MINUTES),
        # 僵死回收：启动对账只救 worker 重启的孤儿；运行中途丢任务（LLM 调用悬死、
        # ARQ 指数延迟重试）的僵死靠周期兜底（判据=终端 30 分钟无动静，见 tasks.py）
        cron(reconcile_stale_voyages, minute={5, 15, 25, 35, 45, 55}),
        # paused_ask 没有 worker 持有；单独巡检长时间无人回答且仍占用 GPU 的命令。
        cron(
            watch_unanswered_managed_commands,
            minute={2, 7, 12, 17, 22, 27, 32, 37, 42, 47, 52, 57},
        ),
    ]
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
    # 其余任务保持 1h 上限
    job_timeout = 3600
    # 启动对账：认领无人执行的 executing 航程（重启/超时把任务弄丢时自动恢复）
    on_startup = reconcile_stuck_voyages


def registered_function_names() -> set[str]:
    """WorkerSettings.functions 里实际注册的任务名。"""
    return {getattr(f, "name", None) or f.__name__ for f in WorkerSettings.functions}


# 与入队方的白名单双向核对，导入即校验：任一边漏了都会让入队被 arq 静默丢弃（#216）。
# 用显式抛错而不是 assert —— assert 在 python -O 下会被剥掉，那正是它最该生效的场合。
_registered = registered_function_names()
if _registered != WORKER_FUNCTIONS:
    raise RuntimeError(
        "WorkerSettings.functions 与 app.core.queue.WORKER_FUNCTIONS 不一致："
        f"只在 worker 侧={sorted(_registered - WORKER_FUNCTIONS)}，"
        f"只在白名单侧={sorted(WORKER_FUNCTIONS - _registered)}"
    )
