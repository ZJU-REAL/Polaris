"""SSH 凭据 schema（docs/api-m4.md §1）。Read 模型绝不包含私钥/口令。"""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class SSHCredentialCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    host: str = Field(min_length=1, max_length=255)
    port: int = Field(default=22, ge=1, le=65535)
    username: str = Field(min_length=1, max_length=255)
    private_key: str = Field(min_length=1)  # PEM 文本，Fernet 加密后入库
    passphrase: str | None = None


class SSHCredentialRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    host: str
    port: int
    username: str
    created_at: datetime
    last_verified_at: datetime | None


class SSHTestResult(BaseModel):
    ok: bool
    detail: str
