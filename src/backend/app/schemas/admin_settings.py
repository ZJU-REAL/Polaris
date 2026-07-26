"""管理端全局设置 schema（system_settings 表读写）。"""

from typing import Literal

from pydantic import BaseModel

AffiliationMode = Literal["on_add", "on_compile"]


class AffiliationModeRead(BaseModel):
    mode: AffiliationMode


class AffiliationModeUpdate(BaseModel):
    mode: AffiliationMode


class PaperEmbeddingRead(BaseModel):
    """平台是否给论文建论文级向量（默认开）。关掉后语义检索只能命中已有向量的论文。"""

    enabled: bool


class PaperEmbeddingUpdate(BaseModel):
    enabled: bool


class LabLeaderboardSettingRead(BaseModel):
    """用量排行榜是否对普通成员可见（默认开；关掉后只有管理员看得到）。"""

    enabled: bool


class LabLeaderboardSettingUpdate(BaseModel):
    enabled: bool


class DailyEmbedBackfillResult(BaseModel):
    """一次性补建向量的结果：本次新建 / 已有跳过 / 未成功。"""

    embedded: int
    skipped: int
    failed: int = 0
