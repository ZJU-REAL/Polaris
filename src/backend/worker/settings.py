"""ARQ WorkerSettings：``arq worker.settings.WorkerSettings`` 启动。"""

from arq import cron, func
from arq.connections import RedisSettings

from app.core.config import get_settings
from app.services.ingest import DAILY_SYNC_UTC_HOUR, DAILY_SYNC_UTC_MINUTE
from worker.tasks import (
    daily_publication_match,
    daily_wiki_ingest,
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
    ]
    # 每日 03:00 对 cadence=daily 且已 bootstrap 的项目触发增量 ingest（docs/api-m2.md §4）
    cron_jobs = [
        cron(daily_wiki_ingest, hour=DAILY_SYNC_UTC_HOUR, minute=DAILY_SYNC_UTC_MINUTE),
        # 每日 ingest 后一小时跑发表匹配（新入库论文 → 姓名+机构命中进待确认）
        cron(daily_publication_match, hour=DAILY_SYNC_UTC_HOUR + 1, minute=DAILY_SYNC_UTC_MINUTE),
    ]
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
    # 其余任务保持 1h 上限
    job_timeout = 3600
    # 启动对账：认领无人执行的 executing 航程（重启/超时把任务弄丢时自动恢复）
    on_startup = reconcile_stuck_voyages
