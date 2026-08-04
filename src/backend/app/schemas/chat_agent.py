"""全局助手的请求/响应模型。"""

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.models.conversation import CONVERSATION_SCOPES


class ConversationCreate(BaseModel):
    scope_kind: str = "global"
    scope_id: uuid.UUID | None = None
    project_id: uuid.UUID | None = None

    def model_post_init(self, _context: Any) -> None:
        if self.scope_kind not in CONVERSATION_SCOPES:
            raise ValueError(f"unknown scope_kind: {self.scope_kind}")


class ConversationRead(BaseModel):
    id: uuid.UUID
    scope_kind: str
    scope_id: uuid.UUID | None = None
    project_id: uuid.UUID | None = None
    title: str
    usage: dict[str, Any] = Field(default_factory=dict)
    last_message_at: datetime | None = None


class ConversationTurnRequest(BaseModel):
    question: str = Field(min_length=1)
    #: 这轮工具检索的课题。全局会话第一次提问时带上；会话上存过就不用再传。
    project_id: uuid.UUID | None = None
    #: 不给就用默认白名单（首期 6 个只读工具）
    tool_names: list[str] | None = None
    max_rounds: int = Field(default=8, ge=1, le=20)
    statement: str | None = None


class MessageRead(BaseModel):
    id: uuid.UUID
    seq: int
    role: str
    kind: str
    blocks: list[Any] = Field(default_factory=list)
    text: str
    status: str
    sources: list[Any] | None = None
    usage: dict[str, Any] | None = None
    stop_reason: str | None = None
    created_at: datetime


class SkillImportRequest(BaseModel):
    """从 SKILL.md 导入技能。files 是附件（相对路径 → 文本内容）。"""

    skill_md: str = Field(min_length=1)
    files: dict[str, str] | None = None
