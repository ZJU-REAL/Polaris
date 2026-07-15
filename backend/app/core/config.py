"""应用配置：pydantic-settings，环境变量前缀 ``POLARIS_``（见仓库根 .env.example）。"""

import logging
from functools import lru_cache
from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger("polaris.config")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="POLARIS_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ---- App ----
    env: Literal["dev", "prod"] = "dev"
    secret_key: str = "dev-only-secret-key-change-me"  # JWT 签名
    encryption_key: str = ""  # Fernet key；为空时 security.py 会从 secret_key 派生（仅限 dev）
    invite_code: str = "polaris-lab"  # 注册邀请码（实验室内部制）

    # ---- Database / Cache ----
    # 默认回退 sqlite+aiosqlite，便于无 docker 的本地开发与测试；生产用 postgresql+asyncpg
    database_url: str = "sqlite+aiosqlite:///./polaris_dev.db"
    redis_url: str = "redis://localhost:6379/0"

    # ---- LLM providers（初始值；后续可在 DB 模型路由表中配置）----
    openai_compat_base_url: str = "https://api.deepseek.com/v1"
    openai_compat_api_key: str = ""
    anthropic_api_key: str = ""

    # ---- 文献 API ----
    s2_api_key: str = ""  # Semantic Scholar（可空，限流更严）
    openalex_mailto: str = "polaris@example.org"  # OpenAlex polite pool

    # ---- 文件卷（PDF/全文等产物；容器内挂 /srv/data）----
    data_dir: str = "./data"
    # 文献 API（arXiv/S2/OpenAlex）出站代理，如 http://host.docker.internal:7897；
    # LLM/内网服务不走此代理
    outbound_proxy: str | None = None
    # 实验服务器 pip 镜像源（可选，如 https://pypi.tuna.tsinghua.edu.cn/simple）
    pip_index_url: str = ""

    @field_validator(
        "s2_api_key", "openai_compat_api_key", "anthropic_api_key", "outbound_proxy", mode="before"
    )
    @classmethod
    def _sanitize_token(cls, v: object) -> object:
        """env 文件行内注释误入值（如 docker compose 对空值+注释的解析差异）会把
        '# 注释文字' 当成 token，非 ASCII 进 HTTP 头直接 UnicodeEncodeError——
        这里统一剥离并拒绝明显非法的值。"""
        if not isinstance(v, str):
            return v
        v = v.split(" #", 1)[0].strip()
        if v and (v.startswith("#") or not v.isascii()):
            logger.warning("忽略非法配置值（疑似注释混入）：%r", v[:40])
            return ""
        return v

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")


@lru_cache
def get_settings() -> Settings:
    return Settings()
