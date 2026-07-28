"""ARQ WorkerSettings：``arq worker.settings.WorkerSettings`` 启动。"""

from arq import cron, func
from arq.connections import RedisSettings

from app.core.config import get_settings
from worker.tasks import (
    daily_feed_sync,
    daily_publication_match,
    index_papers_fulltext_task,
    match_user_publications,
    ping_task,
    reconcile_stuck_voyages,
    resume_voyage,
    run_voyage,
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
    ]
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
    # 其余任务保持 1h 上限
    job_timeout = 3600
    # 启动对账：认领无人执行的 executing 航程（重启/超时把任务弄丢时自动恢复）
    on_startup = reconcile_stuck_voyages
