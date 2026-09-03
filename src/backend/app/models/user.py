"""用户：fastapi-users UUID 用户表 + 展示名 / 头像 / 个人设置。

治理字段（role/read_only/llm_access/token_quota/features）已随个人化定位移除
（#614）：单机档位只有一个用户，机器的主人不需要被自己治理。
"""

from typing import Any

from fastapi_users_db_sqlalchemy import SQLAlchemyBaseUserTableUUID
from sqlalchemy import Boolean, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.base import JSONVariant, TimestampMixin


class User(SQLAlchemyBaseUserTableUUID, TimestampMixin, Base):
    __tablename__ = "users"

    display_name: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    # 用户名：小写字母/数字/下划线 3-32 位，全局唯一；可登录（邮箱或用户名二选一）。
    # 可空——老用户没有用户名（不强制回填），新注册必填。
    username: Mapped[str | None] = mapped_column(String(32), unique=True, index=True)
    # 用户名只能在个人设置里改一次：改过一次后锁定。
    username_locked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # 头像文件（<data_dir>/avatars/<user_id>.<ext>），None = 未上传
    avatar_path: Mapped[str | None] = mapped_column(String(1024))
    # 用户个人设置：{key: value}。None/缺键 = 未设置。
    # managed_command_unanswered_minutes 存远端命令等待用户答复的个人偏好；管理员全局
    # 上限仍优先。历史 chat_fulltext_index 已废除，存量值保留但不再读取。
    settings: Mapped[dict[str, Any] | None] = mapped_column(JSONVariant)

    @property
    def has_avatar(self) -> bool:
        return bool(self.avatar_path)

    def setting(self, key: str, default: Any = None) -> Any:
        if not self.settings:
            return default
        return self.settings.get(key, default)
