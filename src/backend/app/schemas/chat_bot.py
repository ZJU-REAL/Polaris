"""群机器人配置与单向推送 API schema。"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

ChatBotPlatform = Literal["dingtalk", "feishu"]


class ChatBotConfigUpsert(BaseModel):
    # 可填完整官方 Webhook，也可只填其中的 access_token / hook id。
    robot_id: str = Field(min_length=8, max_length=2048)
    secret: str | None = Field(default=None, max_length=512)

    @field_validator("robot_id", mode="before")
    @classmethod
    def clean_robot_id(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value

    @field_validator("secret", mode="before")
    @classmethod
    def clean_secret(cls, value: object) -> object:
        if value is None:
            return None
        if isinstance(value, str):
            cleaned = value.strip()
            return cleaned or None
        return value


class ChatBotConfigRead(BaseModel):
    platform: ChatBotPlatform
    configured: bool
    has_secret: bool
    updated_at: datetime | None = None
    last_delivered_at: datetime | None = None


class ChatBotMessageCreate(BaseModel):
    title: str = Field(default="Polaris 分享", min_length=1, max_length=200)
    # 服务层会按平台单请求上限拆段；总长仍设硬上限，避免一次请求无限占用连接。
    text: str = Field(min_length=1, max_length=50_000)
    link: str | None = Field(default=None, max_length=2048, pattern=r"^https?://")

    @field_validator("title", "text", mode="before")
    @classmethod
    def clean_required_text(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value


class ChatBotDeliveryRead(BaseModel):
    platform: ChatBotPlatform
    delivered: bool
    parts: int = Field(ge=1)
