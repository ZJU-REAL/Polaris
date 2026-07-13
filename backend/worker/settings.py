"""ARQ WorkerSettings：``arq worker.settings.WorkerSettings`` 启动。"""

from arq.connections import RedisSettings

from app.core.config import get_settings
from worker.tasks import ping_task


class WorkerSettings:
    functions = [ping_task]
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
