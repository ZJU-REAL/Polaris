"""应用配置：pydantic-settings，环境变量前缀 ``POLARIS_``（见仓库根 .env.example）。"""

import logging
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger("polaris.config")

#: 早期版本所有安装共用的公开默认 secret_key。新安装由桌面外壳生成独立密钥（#850），
#: 这里留着它只为两件事：解开用它派生的旧密文（core/security.py），以及认出用它算的
#: 旧浏览器扩展 key 摘要（api/download_client.py）。
LEGACY_DEFAULT_SECRET_KEY = "dev-only-secret-key-change-me"

#: Electron 桌面客户端的固定 origin：页面由自定义 app:// scheme 加载（见 src/desktop）。
#: 网页无法伪造自定义 scheme 的 Origin 头。
DESKTOP_ORIGIN = "app://polaris"

#: 引擎只监听回环地址，Host 头只认这几个名字（任何端口）。挡 DNS rebinding：
#: 恶意域名解析到 127.0.0.1 后，浏览器发来的 Host 仍是那个恶意域名。
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="POLARIS_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ---- App ----
    env: Literal["dev", "prod"] = "dev"
    # 只有一种运行形态（#842）：单进程本地引擎——SQLite + 进程内任务队列 + 进程内
    # redis 替身 + 进程内定时任务，由桌面内核拉起，机器上不需要任何外部服务。
    # 以前的 server 档位（uvicorn + arq worker + 外部 Redis/Postgres）已删除。
    # JWT 签名 / 下载链接签名 / 扩展 key 摘要。桌面外壳为每份安装生成独立值经
    # POLARIS_SECRET_KEY 传入（#850）；默认值是公开的，只适合从源码跑的开发环境。
    secret_key: str = LEGACY_DEFAULT_SECRET_KEY
    # Fernet key；桌面外壳为每份安装生成（POLARIS_ENCRYPTION_KEY）。为空时 security.py
    # 从 secret_key 派生（仅限 dev）。
    encryption_key: str = ""
    # 本次启动的本地会话口令（POLARIS_LOCAL_SESSION_SECRET）：桌面外壳每次启动随机生成，
    # 同时交给引擎和自己的渲染进程；设置了它，/auth/local-session 只认带着同一口令的请求，
    # 别的网页、本机其他用户都拿不到会话。不设（从源码跑、测试）时改按 Origin 判断。
    local_session_secret: str = ""
    # 本次启动的实例标识（POLARIS_INSTANCE_ID），由 /api/health 原样返回：桌面外壳靠它
    # 确认 18080 上应答的是自己刚拉起的引擎，而不是残留的旧引擎或别的程序。
    instance_id: str = ""
    # Host 头白名单的追加项（逗号分隔，任何端口）。回环地址（127.0.0.1/localhost/[::1]）
    # 恒在白名单内；"*" 关闭检查（仅限明确知道自己在做什么的调试场景）。
    allowed_hosts: str = ""
    # 本地会话（/auth/local-session 签发的 JWT）有效期，默认 30 天。过期了前端
    # 收到 401 会自动重新取一次，用户无感；想收紧就调小这个值（单位：秒）。
    session_lifetime_seconds: int = 60 * 60 * 24 * 30
    # stdio MCP 没有 HTTP 请求 URL；需要返回绝对图片链接时通过这里提供服务根地址。
    # HTTP MCP 始终复用当前 /mcp 请求的 origin，不读取此项。
    public_base_url: str = ""
    mcp_download_link_ttl_seconds: int = 15 * 60
    structured_content_link_ttl_seconds: int = 5 * 60
    # 额外放行的跨域来源（逗号分隔，完整 origin，如 http://localhost:5173）。桌面客户端的
    # app://polaris 恒在白名单内、无需配置；任何环境都不再放行 "*"（#850）。vite 开发服务器
    # 经代理同源访问引擎，通常也不需要配；需要从别的页面直连引擎（如浏览器里的 MCP 调试器）
    # 时才在这里加。用逗号分隔的 str 而非 list[str]：pydantic-settings 对 list[str]
    # 要求 env 值是 JSON 字面量（'["a","b"]'），与本仓库 .env 的朴素风格不兼容。
    cors_origins: str = ""

    # ---- GitHub ----
    # 上游仓库 owner/name（同 .github/ 里的 remote，不是机构品牌文案）。
    # 反馈改为前端直开 GitHub new-issue 页（#617）后，后端不再代建 issue，
    # PAT（github_token）随之删除；当前后端没有本字段的消费方，保留默认值
    # 是给自建部署将来需要「服务端知道自己上游仓库」的功能留位。
    # 注意：前端拼 new-issue 链接用的是自己写死的 REPO_URL 常量，不读这里。
    github_repo: str = "ZJU-REAL/Polaris"

    # ---- Database / Cache ----
    # SQLite（桌面引导方会传入 userData 下的路径）
    database_url: str = "sqlite+aiosqlite:///./polaris_dev.db"
    # 连接池要装得下进程内任务的并发：最多 inline_max_voyages 条航程同时跑，每条航程的
    # 打分并发（_LLM_CONCURRENCY=5）每篇论文各开一个 session，再加上引擎自己记步骤/检查点
    # 的那条，以及 HTTP 请求。SQLAlchemy 默认 5+10=15，在 ARQ 时代（max_jobs=10）实测被
    # 打满：多数协程等满 30s 超时、该篇打分失败、状态停在 candidate。SQLite 开了 WAL，
    # 多连接并发读不互斥，写入由 busy_timeout 排队（core/db.py）。
    db_pool_size: int = 20
    db_max_overflow: int = 50
    db_pool_timeout: int = 30
    # SQLite 写锁的等待上限（毫秒）：写入撞上别的连接在写时排队等，而不是立刻
    # 报 "database is locked"。
    sqlite_busy_timeout_ms: int = 30_000

    # ---- 进程内任务队列（core/queue.py） ----
    # 航程与其它后台任务（导入、导出、翻译、解析……）各自的并发上限。航程常常一跑几小时
    # （实验轮询），单独一个池子，免得几条长实验把短任务全部挡在门外。
    inline_max_voyages: int = 8
    inline_max_jobs: int = 6

    # 语义检索在 Python 侧打分（services/vector_search.py），一次扫的行数要有上限。
    # 段落/全文检索的候选行超过这个数（典型：全局助手跨全部文献库搜段落）时，先用
    # 论文级向量挑出最相关的 vector_search_narrow_papers 篇，再只在这些论文里搜段落。
    vector_search_row_budget: int = 20000
    vector_search_narrow_papers: int = 200

    # ---- LLM providers ----
    # 服务商与密钥在管理页配置、存 DB（services/llm_admin.py）；这里只有 openai_compat
    # 路由未填 base_url 时的兜底地址。密钥类环境变量从未有读取点，已删（#629）。
    openai_compat_base_url: str = "https://api.deepseek.com/v1"
    # 未配置任何 LLM 路由时是否回退内置 fake provider——**严格显式 opt-in**（#717）。
    # 任何档位、任何 env：不显式设 POLARIS_LLM_FAKE_FALLBACK=1 就绝不回退，
    # 未配置时 AI 功能返回 LLM_NOT_CONFIGURED，而不是产出演示假内容。
    # 依据设计报告 §18 的信任设计：AI 输出必须真实可溯源，拿不出真结果就明说
    # ——把编造内容当真话发给用户是对信任的根本破坏。产品自身（含桌面端
    # legacy-engine 插件）永远不设这个变量；只有测试套件与无 key 演示会显式开。
    # 不再按 env 分支强关：曾经的 prod-only 守卫在 desktop 档（与 prod 互斥）
    # 永不触发，等于形同虚设；与其靠猜环境，不如统一信任「显式设置」这一个来源。
    llm_fake_fallback: bool = False
    #: 全局助手（Claude Code 式工具循环）。默认关：它每轮都要重发历史与工具 schema，
    #: 成本与现有一次性对话不是一个量级，先按部署开。
    chat_agent_enabled: bool = False

    # ---- 文献 API ----
    s2_api_key: str = Field(
        default="",
        validation_alias=AliasChoices(
            "POLARIS_S2_API_KEY",
            "SEMANTIC_SCHOLAR_API_KEY",
            "PAPER_SEARCH_MCP_SEMANTIC_SCHOLAR_API_KEY",
        ),
    )  # Semantic Scholar（可空，限流更严）
    openalex_mailto: str = Field(
        default="polaris@example.org",
        validation_alias=AliasChoices("POLARIS_OPENALEX_MAILTO", "OPENALEX_MAILTO"),
    )  # OpenAlex polite pool
    # 新论文补全时顺带做 OpenAlex 对齐（#639）。默认开；测试套件置 0——
    # 补全钩子里的对齐是真实出网调用，离线跑测试不该碰它（专测对齐的用例
    # 自己注入 mock 客户端并临时打开）。
    openalex_align_on_enrich: bool = True
    pubmed_email: str = Field(
        default="",
        validation_alias=AliasChoices(
            "POLARIS_PUBMED_EMAIL", "PUBMED_EMAIL", "PAPER_SEARCH_MCP_UNPAYWALL_EMAIL"
        ),
    )
    pubmed_api_key: str = Field(
        default="",
        validation_alias=AliasChoices(
            "POLARIS_PUBMED_API_KEY", "PUBMED_API_KEY", "PAPER_SEARCH_MCP_PUBMED_API_KEY"
        ),
    )
    crossref_mailto: str = Field(
        default="",
        validation_alias=AliasChoices("POLARIS_CROSSREF_MAILTO", "CROSSREF_MAILTO"),
    )
    core_api_key: str = Field(
        default="",
        validation_alias=AliasChoices(
            "POLARIS_CORE_API_KEY", "CORE_API_KEY", "PAPER_SEARCH_MCP_CORE_API_KEY"
        ),
    )
    unpaywall_email: str = Field(
        default="",
        validation_alias=AliasChoices(
            "POLARIS_UNPAYWALL_EMAIL", "UNPAYWALL_EMAIL", "PAPER_SEARCH_MCP_UNPAYWALL_EMAIL"
        ),
    )
    sciverse_base_url: str = Field(
        default="https://api.sciverse.space",
        validation_alias=AliasChoices("POLARIS_SCIVERSE_BASE_URL", "SCIVERSE_BASE_URL"),
    )
    sciverse_api_tokens: str = Field(
        default="",
        validation_alias=AliasChoices(
            "POLARIS_SCIVERSE_API_TOKENS", "SCIVERSE_API_TOKENS", "SCIVERSE_API_TOKEN"
        ),
    )
    literature_source_concurrency: int = Field(
        default=4,
        ge=1,
        le=32,
        validation_alias=AliasChoices(
            "POLARIS_LITERATURE_SOURCE_CONCURRENCY", "PAPER_SEARCH_SOURCE_CONCURRENCY"
        ),
    )
    literature_source_timeout_seconds: float = Field(
        default=25.0,
        gt=0,
        le=300,
        validation_alias=AliasChoices(
            "POLARIS_LITERATURE_SOURCE_TIMEOUT_SECONDS", "PAPER_SEARCH_SOURCE_TIMEOUT_SECONDS"
        ),
    )
    literature_source_retries: int = Field(
        default=2,
        ge=0,
        le=5,
        validation_alias=AliasChoices(
            "POLARIS_LITERATURE_SOURCE_RETRIES", "PAPER_SEARCH_SOURCE_RETRIES"
        ),
    )
    venue_metrics_cache_ttl_days: int = Field(default=30, ge=1, le=3650)
    venue_metrics_concurrency: int = Field(default=4, ge=1, le=16)
    easyscholar_base_url: str = Field(
        default="https://www.easyscholar.cc/open/getPublicationRank",
        validation_alias=AliasChoices("POLARIS_EASYSCHOLAR_BASE_URL", "EASYSCHOLAR_BASE_URL"),
    )
    easyscholar_secret_keys: str = Field(
        default="",
        validation_alias=AliasChoices("POLARIS_EASYSCHOLAR_SECRET_KEYS", "EASYSCHOLAR_SECRET_KEYS"),
    )
    mineru_base_url: str = Field(
        default="https://mineru.net/api/v4",
        validation_alias=AliasChoices("POLARIS_MINERU_BASE_URL", "MINERU_BASE_URL"),
    )
    mineru_api_tokens: str = Field(
        default="",
        validation_alias=AliasChoices(
            "POLARIS_MINERU_API_TOKENS", "MINERU_API_TOKENS", "MINERU_API_KEY"
        ),
    )
    mineru_timeout_seconds: float = Field(
        default=3600.0,
        gt=30,
        le=86_400,
        validation_alias=AliasChoices("POLARIS_MINERU_TIMEOUT_SECONDS", "MINERU_TIMEOUT_SECONDS"),
    )
    mineru_poll_interval_seconds: float = Field(
        default=10.0,
        ge=1,
        le=300,
        validation_alias=AliasChoices(
            "POLARIS_MINERU_POLL_INTERVAL_SECONDS", "MINERU_POLL_INTERVAL_SECONDS"
        ),
    )
    mineru_retries: int = Field(
        default=2,
        ge=0,
        le=5,
        validation_alias=AliasChoices("POLARIS_MINERU_RETRIES", "MINERU_RETRIES"),
    )
    mineru_concurrency: int = Field(
        default=2,
        ge=1,
        le=16,
        validation_alias=AliasChoices("POLARIS_MINERU_CONCURRENCY", "MINERU_CONCURRENCY"),
    )

    # ---- 解析双轨适配器（#650，enrich 链 extract 步骤）----
    # 自托管 GROBID / MinerU web_api 的服务地址，空 = 该轨不可用（golden 链路不配，
    # 走纯 PyMuPDF fallback）。注意与上面的 mineru_*（MinerU Cloud 批量接口，版本化
    # PDF 生命周期用）是两套部署形态，互不复用。
    grobid_url: str = Field(
        default="",
        validation_alias=AliasChoices("POLARIS_GROBID_URL", "GROBID_URL"),
    )
    mineru_parse_url: str = Field(
        default="",
        validation_alias=AliasChoices("POLARIS_MINERU_URL", "MINERU_URL"),
    )

    # ---- 文件卷（PDF/全文等产物；容器内挂 /srv/data）----
    data_dir: str = "./data"
    # 常驻文件投影（#719 file-over-app 一期）：在 <data_dir>/workspace/ 下维护一份
    # 用户可见的文件副本（PDF 别名 / 笔记 md / wiki vault）。DB 仍是唯一真源，
    # 关掉只是停止刷新文件，不影响任何业务功能（测试套件与 golden 环境关）。
    file_projection: bool = True
    # 文献 API（arXiv/S2/OpenAlex）出站代理，如 http://host.docker.internal:7897；
    # LLM/内网服务不走此代理
    outbound_proxy: str | None = None
    # 实验服务器 pip 镜像源（可选，如 https://pypi.tuna.tsinghua.edu.cn/simple）
    pip_index_url: str = ""

    @field_validator(
        "s2_api_key",
        "pubmed_api_key",
        "core_api_key",
        "sciverse_api_tokens",
        "mineru_api_tokens",
        "outbound_proxy",
        mode="before",
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

    @field_validator("data_dir", mode="after")
    @classmethod
    def _resolve_data_dir(cls, v: str) -> str:
        """data_dir 一律钉成绝对路径（#718）：桌面引擎启动器会 chdir 到
        App 安装包里的后端源码目录，相对默认 "./data" 会随之漂进安装目录
        （应用更新即被整体替换，用户文件全丢）。这里在 settings 加载时基于
        当时的 cwd resolve 一次，此后进程内任何 chdir 都不再影响落盘位置；
        桌面正常路径由引导器注入绝对的 POLARIS_DATA_DIR，本兜底只在 env
        缺失时生效。docker/server 部署显式设 /srv/data（绝对路径），不受影响。"""
        path = Path(v)
        if not path.is_absolute():
            resolved = str(path.resolve())
            logger.info("data_dir 是相对路径 %r，已按当前工作目录解析为 %s", v, resolved)
            return resolved
        return v

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def allowed_origin_list(self) -> list[str]:
        """可以跨域调用引擎的全部来源：桌面客户端 + 显式配置的。"""
        return [DESKTOP_ORIGIN, *(o for o in self.cors_origin_list if o != DESKTOP_ORIGIN)]

    @property
    def allowed_host_list(self) -> list[str]:
        """Host 头白名单（小写、不含端口）。含 "*" 表示不检查。"""
        extra = [h.strip().lower().strip("[]") for h in self.allowed_hosts.split(",") if h.strip()]
        return [*LOOPBACK_HOSTS, *(h for h in extra if h not in LOOPBACK_HOSTS)]


@lru_cache
def get_settings() -> Settings:
    return Settings()
