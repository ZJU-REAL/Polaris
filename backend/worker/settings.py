"""ARQ WorkerSettings：``arq worker.settings.WorkerSettings`` 启动。"""

from arq.connections import RedisSettings

from app.core.config import get_settings
from worker.tasks import ping_task, resume_voyage, run_voyage


class WorkerSettings:
    functions = [ping_task, run_voyage, resume_voyage]
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
    # 航程可能长时间运行（LLM 多步调用），放宽单任务超时
    job_timeout = 3600
