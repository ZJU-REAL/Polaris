"""管理端全局设置 schema（system_settings 表读写）。"""

from typing import Literal

from pydantic import BaseModel

AffiliationMode = Literal["on_add", "on_compile"]


class AffiliationModeRead(BaseModel):
    mode: AffiliationMode


class AffiliationModeUpdate(BaseModel):
    mode: AffiliationMode


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


class EmbeddingSpaceItem(BaseModel):
    """库里出现过的一个向量空间及其规模。"""

    key: str
    model: str
    dim: int
    papers: int
    chunks: int
    ideas: int
    active: bool


class EmbeddingSpaceStatus(BaseModel):
    """当前向量模型与库里各空间的分布。

    ``routed_model`` 是路由表里配的嵌入模型；它与 ``active`` 的模型不一致时，
    说明管理员换过模型而索引还没重建——此时所有嵌入调用都会拒绝执行，直到
    调用 adopt 接口确认换用新模型（旧向量保留，可切回）。
    """

    active: EmbeddingSpaceItem | None = None
    routed_model: str | None = None
    mismatched: bool = False
    spaces: list[EmbeddingSpaceItem] = []


class EmbeddingSpaceAdoptResult(BaseModel):
    """切换激活空间的结果：切到哪个、原先是哪个。"""

    active: EmbeddingSpaceItem
    previous: str | None = None


class ExperimentEnvSettings(BaseModel):
    """实验的全局环境设置（所有实验共用一份）。

    空字符串 = 不配置该项，各自走兜底行为（不配 pip 镜像就用官方源，等等）。
    路径/URL 会拼进远端 shell，服务层做白名单校验，非法值直接拒收。
    """

    model_root: str = ""  # 本机模型根目录，如 /hf/model
    dataset_root: str = ""  # 本机数据集根目录
    pip_index_url: str = ""  # pip 镜像源
    hf_endpoint: str = ""  # HF 镜像端点，如 https://hf-mirror.com
    proxy_url: str = ""  # 实验机出外网的 HTTP 代理
