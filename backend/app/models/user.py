"""用户：fastapi-users UUID 用户表 + 展示名 / 角色 / 头像 / 配额与功能权限。"""

from typing import Any

from fastapi_users_db_sqlalchemy import SQLAlchemyBaseUserTableUUID
from sqlalchemy import BigInteger, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.base import JSONVariant, TimestampMixin

# 功能权限键（缺省/None = 允许；显式 false = 禁用对应阶段的发起入口）
FEATURE_KEYS = ("forge", "review", "experiment", "writer", "paper_review")


class User(SQLAlchemyBaseUserTableUUID, TimestampMixin, Base):
    __tablename__ = "users"

    display_name: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    role: Mapped[str] = mapped_column(String(32), default="member", nullable=False)  # admin|member
    # 大模型使用权限：full=不限 | chat_only=仅文献对话与 AI 伴读 | blocked=锁定
    llm_access: Mapped[str] = mapped_column(String(16), default="full", nullable=False)
    # 头像文件（<data_dir>/avatars/<user_id>.<ext>），None = 未上传
    avatar_path: Mapped[str | None] = mapped_column(String(1024))
    # LLM token 配额（prompt+completion 累计）；None = 不限
    token_quota: Mapped[int | None] = mapped_column(BigInteger)
    # 功能权限：{feature: bool}；None/缺键 = 允许
    features: Mapped[dict[str, Any] | None] = mapped_column(JSONVariant)

    @property
    def has_avatar(self) -> bool:
        return bool(self.avatar_path)

    def feature_enabled(self, feature: str) -> bool:
        if not self.features:
            return True
        return bool(self.features.get(feature, True))
