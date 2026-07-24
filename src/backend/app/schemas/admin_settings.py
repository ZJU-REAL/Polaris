"""管理端全局设置 schema（system_settings 表读写）。"""

from typing import Literal

from pydantic import BaseModel

AffiliationMode = Literal["on_add", "on_compile"]


class AffiliationModeRead(BaseModel):
    mode: AffiliationMode


class AffiliationModeUpdate(BaseModel):
    mode: AffiliationMode


class DailyEmbedRead(BaseModel):
    """每日新论文是否自动建向量（开了才能做语义检索；默认关）。"""

    enabled: bool


class DailyEmbedUpdate(BaseModel):
    enabled: bool


class DailyEmbedBackfillResult(BaseModel):
    """一次性补建向量的结果：本次新建 / 已有跳过 / 未成功。"""

    embedded: int
    skipped: int
    failed: int = 0
