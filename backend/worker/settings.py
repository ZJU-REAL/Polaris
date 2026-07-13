"""ARQ WorkerSettings：``arq worker.settings.WorkerSettings`` 启动。"""

from arq import cron
from arq.connections import RedisSettings

from app.core.config import get_settings
from worker.tasks import daily_wiki_ingest, ping_task, resume_voyage, run_voyage


class WorkerSettings:
    functions = [ping_task, run_voyage, resume_voyage]
    # 每日 03:00 对 cadence=daily 且已 bootstrap 的项目触发增量 ingest（docs/api-m2.md §4）
    cron_jobs = [cron(daily_wiki_ingest, hour=3, minute=0)]
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
    # 航程可能长时间运行（LLM 多步调用），放宽单任务超时
    job_timeout = 3600
