"""每用户私有的群机器人 Webhook 配置（凭据加密入库）。"""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.base import TimestampMixin, UUIDPrimaryKeyMixin


class ChatBotConfig(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """钉钉/飞书群自定义机器人；一个用户每个平台至多一条。"""

    __tablename__ = "chat_bot_configs"
    __table_args__ = (
        UniqueConstraint("user_id", "platform", name="uq_chat_bot_configs_user_platform"),
        CheckConstraint(
            "platform IN ('dingtalk', 'feishu')", name="ck_chat_bot_configs_platform"
        ),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    platform: Mapped[str] = mapped_column(String(16), nullable=False)
    # Webhook 中的 access_token / hook id 与签名密钥都能直接授权发群消息，均加密落库。
    robot_id_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    secret_encrypted: Mapped[str | None] = mapped_column(Text)
    last_delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
