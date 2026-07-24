"""管理端全局设置 schema（system_settings 表读写）。"""

from typing import Literal

from pydantic import BaseModel

AffiliationMode = Literal["on_add", "on_compile"]


class AffiliationModeRead(BaseModel):
    mode: AffiliationMode


class AffiliationModeUpdate(BaseModel):
    mode: AffiliationMode
