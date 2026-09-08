"""系统级 KV：只放平台运维状态与部署级机器事实（#737 配置分层后收窄）。

用户偏好（每日订阅分类/抓取时刻/保留天数/TTS 全局档/机构抽取模式等）已迁到
``users.settings`` 的命名空间键（见 services/owner_settings.py）；旧键只作迁移期
只读回退。仍留在这张表里的是「不属于任何用户」的东西：

- 机器状态/派生缓存：daily_feed_probe_state、claim_today 占位、daily_library_anchor:*、
  embedding_active_space（激活向量空间是索引状态，换错了检索全乱）；
- 运维旋钮与护栏：daily_feed_max_probe_attempts（对 arXiv 的限流节奏）、
  llm_call_logging_enabled（诊断开关）、managed_command_watchdog（管理员上限，
  用来约束用户偏好，本身不能是用户偏好）；
- 部署级事实与密钥：experiment_env（实验机的路径/镜像约定）、literature_search 与
  document_processing（用户可调项和加密凭据同住一份原子文档，advisory lock 串行化
  读改写；拆开迁移会破坏原子性，归属有争议——保守留原层，见 #737）。
"""

from typing import Any

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.base import JSONVariant, TimestampMixin


class SystemSetting(TimestampMixin, Base):
    __tablename__ = "system_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[Any | None] = mapped_column(JSONVariant, nullable=True)
