"""alembic 迁移 sqlite 实跑：全链 upgrade head + 最新 revision 往返。"""

from pathlib import Path

from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from alembic import command

BACKEND_DIR = Path(__file__).resolve().parent.parent

HEAD_REVISION = "9a4c2e7b1d58"  # SQLite：voyage_terminal_logs.id 改成 INTEGER 主键
TERMINAL_LOG_PK_REVISION = HEAD_REVISION
USER_ROWS_MERGE_REVISION = "7d2e4f9a1b63"  # 单用户：别的用户行的数据并到本地用户 (#850)
SINGLE_USER_REVISION = "3c7d9e1f5a20"  # 单用户：LLM 配置并成一张表、agent 去掉共享列 (#842)
NO_ACCOUNTS_REVISION = "88a5bdc0aa15"  # 删掉邮箱验证码表：去掉账号层 (#842)
ROUTES_TO_AGENTS_REVISION = "a9c4e7d2f1b8"  # 路由可以指向外部 agent (#840)
ACP_AGENTS_REVISION = "f6b2d8e04a17"  # 外部 agent（ACP 后端）登记表 (#836)
LEGACY_PREFS_REVISION = "d3f9a1c7e2b4"  # 删掉偏好的旧 system_settings 行 (#821 E2)
DAILY_FEED_SOURCES_REVISION = "b5e2c8d41f7a"  # 每日池条目记来源 (#821)
INPUT_BUDGETS_REVISION = "a8d3e5f71c42"  # 路由的输入预算 (#811)
RERANK_PATH_REVISION = "e3a7c91f20b6"  # provider 的 rerank 路径 (#810)
DAILY_SUBS_REVISION = "c1d80a3fb492"  # 每日订阅按人存 (#806)
SKILLS_DROP_REVISION = "d7f4a16c8e29"  # 技能功能移除 (#755)
VECTOR_SCOPE_REVISION = "c5e02a9b31d7"  # Method vectors scoped to their card (#772)
HYPOTHESIS_SEQ_REVISION = "b4d91f7a2c08"  # Hypothesis node creation sequence (#784)
MCP_SERVERS_REVISION = "e8c3f1a92d40"  # External MCP server registry (#754)
LIBRARY_DISCIPLINE_REVISION = "d7b2e4c81a35"  # Library declares its discipline
CRDT_STATE_REVISION = "c4a1d8e93b57"  # Manuscript CRDT state persistence (#347)
SKILLS_CONVERGENCE_REVISION = "a5b9c3d7e1f2"  # Skill-system convergence step 1 (#741)
HYGIENE_REVISION = "351c324f4f6b"  # Schema hygiene: retired columns + owner merge (#734)
SETTINGS_REVISION = "b737c1a2d3e4"  # User-preference settings move to users.settings (#737)
RESOURCES_REVISION = "d2dcfc8b899f"  # Resources, leases, polymorphic credentials (#677)
METHOD_VECTORS_REVISION = "e867fcbae4ea"  # Method purpose/mechanism vectors (#663)
EXTRACTIONS_REVISION = "58b0bc2d809d"  # Paper skeleton extractions (#661)
CITATIONS_REVISION = "57543f6328a1"  # Paper citation edges (#639)
HYPOTHESIS_TREE_REVISION = "7e2b9f4c1a86"  # Hypothesis/experiment tree nodes (#637)
MEMBERS_DROP_REVISION = "b0dd1709e2a1"  # Drop project_members (#625)
LLM_MERGE_REVISION = "dd572a7f063c"  # Merge LLM config tracks: drop users.llm_self_managed (#621)
LIBRARY_STATUS_DROP_REVISION = "c3c404803ca4"  # Drop library status/review_note (P1 de-lab)
FEEDBACK_DROP_REVISION = "9b2e5d81c7a3"  # Drop in-app feedback tables (#617)
GOVERNANCE_DROP_REVISION = "4f8d2c9b7a61"  # Drop user governance columns (P1 de-lab)
CURATORS_DROP_REVISION = "e6c31f84a2d5"  # Drop library curators (P1 de-lab)
SKILL_RATINGS_DROP_REVISION = "d4b8e26f1a93"  # Drop skill ratings (P1 de-lab)
RANKINGS_DROP_REVISION = "c8f2a61d9e37"  # Drop view events (P1 de-lab)
INVITES_DROP_REVISION = "b7d4f92e6c15"  # Drop project invites (P1 de-lab)
CODES_DROP_REVISION = "a3e9c17b5f42"  # Drop registration codes (P1 de-lab)
TRANSLATIONS_REVISION = "f4a5b6c7d8e9"  # Versioned discovery-hit translations
DISCOVERY_SCHEDULE_REVISION = "f3a4b5c6d7e8"  # Scheduled incremental literature discovery
VENUE_METRIC_REVISION = "f2a3b4c5d6e7"  # Versioned literature venue metrics
SCOPE_VERSION_REVISION = "f1a2b3c4d5e6"  # Immutable interdisciplinary scope versions
QUERY_MATRIX_REVISION = "f0a1b2c3d4e5"  # Interdisciplinary query matrix and evidence balance
INTERDISCIPLINARY_REVISION = "e9f0a1b2c3d4"  # Interdisciplinary research profiles
OA_CACHE_REVISION = "d1e2f3a4b5c6"  # Persistent OA cache and promotion audit
DOWNLOAD_BATCH_REVISION = "e0f1a2b3c4d5"  # Polaris extension download batches and API keys
EVIDENCE_ANCHOR_REVISION = "e5f6a7b8c9d0"  # Version-aware sentence/paragraph evidence anchors
CONTENT_VERSION_REVISION = "d9e0f1a2b3c4"  # Versioned parsed PDF content and vectors
PDF_ASSET_REVISION = "c8d9e0f1a2b3"  # Content-addressed PDF assets and grants
LITERATURE_REVISION = "a7c8d9e0f1b2"  # library-scoped literature discovery contracts
PREVIOUS_HEAD_REVISION = "8ff89f7fcdeb"  # integration tokens
PROVIDER_UA_REVISION = "7b3e91c4a2d8"  # Provider 级可选 User-Agent
VIEW_EVENTS_REVISION = "a1c9e73b5d20"  # 浏览事件（文献库/论文点击量）
VOYAGE_MESSAGES_REVISION = "63133f647463"  # 任务对话流：voyage_messages 表
READ_ONLY_REVISION = "b3f5c1e07a92"  # 只读账号（游客）
SKILLS_GLOBAL_REVISION = "07e7faea4c7a"  # 技能全局启用（user_skills，不再绑定课题）
MEMORY_KIND_REVISION = "d4e8b19c7a55"  # 记忆分层：fact 每轮带上 / note 检索到才回上下文
BUDDY_REVISION = "c31f7a9d40b2"  # Buddy 的长期记忆（用户自己写的）
SKILLS_REVISION = "a22aa895244c"  # Skills v2（SKILL.md 渐进披露）
DIGEST_REVISION = "e6a1c9d4f207"  # 文献库每日简报 + 相关性理由
SCORED_RUN_REVISION = "78e222c38b3b"  # 成员行记下打分它的那次同步任务 id
CONVERSATIONS_REVISION = "581d172bd41b"  # 对话搬到服务端
INLINE_VECTOR_DROP_REVISION = "929c05a03745"  # 删除主表向量列（向量已搬进侧表）
VECTOR_TABLES_REVISION = "5d8ebd5cb100"  # 向量侧表建表 + 存量搬迁
EFFORT_REVISION = "510f6bde2233"  # 模型路由推理档位（model_routes.effort）
CHAT_BOT_REVISION = "d4e8b71c2a90"  # 每用户群机器人 Webhook 配置
CONCEPT_STATUS_REVISION = "a9f1c62b70d5"  # 概念转正门槛（concepts.status）
INDEX_META_REVISION = "c4e7b2a91f38"  # 分段来源标记 + 向量构建元信息
CONCEPTS_REVISION = "b6c2f81d4a09"  # 概念统一到论文级
PREV_REVISION = "a7d0c9e51b34"  # 解读统一到 paper_wikis


def _make_config(db_path: Path) -> Config:
    # 不读取带中文注释的 alembic.ini，避免 Windows locale=GBK 导致迁移测试在
    # 非 UTF-8 环境下失败。迁移运行只需要这两个配置项。
    cfg = Config()
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{db_path}")
    return cfg


def _index_names(db_path: Path, table: str) -> set[str]:
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        with engine.connect() as conn:
            return {ix["name"] for ix in inspect(conn).get_indexes(table)}
    finally:
        engine.dispose()


def _inspect_db(db_path: Path) -> tuple[str, dict[str, set[str]]]:
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        with engine.connect() as conn:
            version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
            inspector = inspect(conn)
            tables = set(inspector.get_table_names())
            columns = {
                table: {c["name"] for c in inspector.get_columns(table)}
                for table in (
                    "papers",
                    "ideas",
                    "review_sessions",
                    "review_messages",
                    "experiments",
                    "experiment_runs",
                    "paper_notes",
                    "paper_tags",
                    "paper_tag_links",
                    "paper_user_meta",
                    "user_paper_tags",
                    "paper_highlights",
                    "manuscripts",
                    "manuscript_files",
                    "manuscript_file_versions",
                    "manuscript_templates",
                    "users",
                    "model_routes",
                    "voyage_runs",
                    "voyage_steps",
                    "llm_providers",
                    "llm_call_logs",
                    "system_settings",
                    "feedback",  # head 已删（#617）；仅 downgrade 断言用
                    "feedback_images",
                    "user_library_entries",
                    "concepts",
                    "paper_chunks",
                    "paper_vectors",
                    "method_vectors",  # head 新增（#663）；downgrade 后不存在，列检查自动跳过
                    # 技能表在 head 上已删（#755），但降级走回旧 revision 时它们还在，
                    # 那几档的列断言要查它们。清单里留着：查不到会自动跳过
                    "skills",
                    "skill_versions",
                    "user_skills",
                    "skill_listings",
                    "agent_skills",
                    "agent_skill_files",
                    "paper_wikis",
                    "library_papers",
                    "daily_feed_entries",
                    "user_publications",
                    "topic_papers",
                    "llm_usage",
                    "topic_source_libraries",
                    "activities",
                    "direction_libraries",
                    "projects",
                    "chat_bot_configs",
                    "library_research_digests",
                    "conversations",
                    "conversation_messages",
                    "guidance_documents",
                    "mcp_servers",
                    "acp_agents",
                    "buddy_memories",
                    "view_events",
                    "integration_tokens",
                    "literature_search_runs",
                    "literature_search_hits",
                    "literature_source_attempts",
                    "literature_venue_metric_cache",
                    "literature_discovery_schedules",
                    "literature_hit_translations",
                    "pdf_blobs",
                    "paper_assets",
                    "asset_grants",
                    "paper_content_versions",
                    "paper_content_chunks",
                    "paper_content_version_vectors",
                    "paper_content_chunk_vectors",
                    "paper_evidence_anchors",
                    "paper_citations",
                    "paper_extractions",
                    "download_api_keys",
                    "download_batches",
                    "download_batch_items",
                    "literature_oa_caches",
                    "literature_oa_attempts",
                    "interdisciplinary_research_profiles",
                    "interdisciplinary_research_profile_versions",
                    "registration_codes",  # head 已删；仅 downgrade 断言用
                    "project_members",  # head 已删（#625）；仅 downgrade 断言用
                    "hypothesis_nodes",
                    "connection_credentials",  # head 新增（#677）：ssh_credentials 改名
                    "ssh_credentials",  # head 已改名（#677）；仅 downgrade 断言用
                    "resources",
                    "resource_leases",
                )
                if table in tables  # downgrade 后新表不存在，跳过列检查
            }
            columns["_tables"] = tables
    finally:
        engine.dispose()
    return version, columns


def test_migrations_sqlite_upgrade_head_and_roundtrip(tmp_path):
    db_path = tmp_path / "migrate.db"
    cfg = _make_config(db_path)

    command.upgrade(cfg, "head")
    version, columns = _inspect_db(db_path)
    assert version == HEAD_REVISION
    # 外部 MCP 服务器登记（#754）：env 整体加密，命令可审计
    assert {"slug", "transport", "env_encrypted", "enabled"} <= columns["mcp_servers"]
    # 外部 agent 登记（#836）：与 MCP 服务器同一套 env 加密约定，外加权限策略；
    # 「共享给其他账号」列随单用户化删掉（#842）
    assert {"slug", "command", "env_encrypted", "permission_policy"} <= columns["acp_agents"]
    assert "shared" not in columns["acp_agents"]
    # 学科包：文献库声明学科，决定本库论文按哪套 schema 抽
    assert "discipline" in columns["direction_libraries"]
    # 协同文档 CRDT 状态（#347）：房间重建靠它，不能只留纯文本投影
    assert "ydoc_state" in columns["manuscript_files"]
    # 列卫生（#734）：退役快照列与归属副本列在 head 已删
    assert not {"wiki_content", "compiled_at", "compiled_model"} & columns["library_papers"]
    assert "wiki_content" not in columns["user_library_entries"]
    assert not {"wiki_content", "wiki_model"} & columns["daily_feed_entries"]
    assert not {"wiki_snapshot", "snapshot_at"} & columns["topic_papers"]
    assert "created_by" not in columns["direction_libraries"]
    assert "submitted_by" in columns["direction_libraries"]
    # 治理列已随个人化定位删除（#614）
    assert not {"role", "read_only", "llm_access", "token_quota", "features"} & columns["users"]
    # 假设/实验树节点表（#637，设计报告 §8.2）
    assert "hypothesis_nodes" in columns["_tables"]
    assert {
        "run_id",
        "parent_id",
        "kind",
        "statement",
        "grounding",
        "novelty_report",
        "feasibility",
        "score",
        "status",
        # run 内创建序号（#784）：恢复顺序不能押在墙钟上
        "seq",
    } <= columns["hypothesis_nodes"]
    assert "ix_hypothesis_nodes_run_id" in _index_names(db_path, "hypothesis_nodes")
    assert "ix_hypothesis_nodes_run_seq" in _index_names(db_path, "hypothesis_nodes")
    # 课题成员表已随个人化定位删除（#625）：归属只看 projects.owner_id
    assert "project_members" not in columns["_tables"]
    assert "owner_id" in columns["projects"]
    assert "effort" in columns["model_routes"]  # 推理档位可配（NULL = 用模型默认）
    # 对话搬到服务端：agent 一轮里可能调好几次工具，历史不能只活在浏览器 localStorage
    assert {"conversations", "conversation_messages"} <= columns["_tables"]
    # Skills v2：技能是「一句 description 常驻 + 正文按需加载」，附件单独一张表
    # 技能功能整体移除（#755）：两套表都不该再存在
    assert not {"agent_skills", "agent_skill_files"} & columns["_tables"]
    assert {"scope_kind", "scope_id", "usage", "active_stream_id"} <= columns["conversations"]
    assert {"blocks", "text", "seq", "status", "sources"} <= columns["conversation_messages"]
    # deprecated（#734 停写）：列保留一期防在途回滚，下一次列卫生迁移删除
    assert "conversation_id" in columns["llm_usage"]
    assert "venue_metric_snapshot" in columns["literature_search_hits"]
    assert {"provider", "identity_key", "metrics", "expires_at"} <= columns[
        "literature_venue_metric_cache"
    ]
    assert {"trigger", "schedule_version", "scheduled_for"} <= columns["literature_search_runs"]
    assert {
        "library_id",
        "enabled",
        "timezone",
        "requested_count",
        "config_version",
        "next_run_at",
    } <= columns["literature_discovery_schedules"]
    assert {
        "hit_id",
        "target_language",
        "source_hash",
        "model_version",
        "status",
        "translated_fields",
        "requested_by",
    } <= columns["literature_hit_translations"]
    # 压缩阈值要知道模型的窗口有多大，此前 router 只能拍脑袋
    assert "context_window" in columns["model_routes"]
    # 向量搬进三张侧表，主表上的向量列与元信息列一并删除
    assert {"paper_vectors", "paper_chunk_vectors", "idea_vectors"} <= columns["_tables"]
    assert {"paper_id", "space", "dim", "embedding", "model", "built_at"} <= columns[
        "paper_vectors"
    ]
    assert "embedding" not in columns["papers"]
    assert not {"embedding_model", "embedding_at"} & columns["papers"]
    assert not {"chunk_embedding_model", "chunk_embedding_at"} & columns["papers"]
    assert "embedding" not in columns["paper_chunks"]
    assert "embedding" not in columns["ideas"]
    assert "source" in columns["paper_chunks"]  # 分段来源标记
    # M3 列仍在
    assert {"score_rationale", "matches", "wins"} <= columns["ideas"]
    assert "payload" in columns["review_sessions"]
    assert "author_name" in columns["review_messages"]
    assert "agent_persona" not in columns["review_messages"]
    # M4 的 ssh_credentials 在 head 已泛化改名为 connection_credentials（#677）
    assert "ssh_credentials" not in columns["_tables"]
    assert "connection_credentials" in columns["_tables"]
    assert {"kind", "payload_encrypted", "private_key_encrypted", "proxy_url"} <= columns[
        "connection_credentials"
    ]
    assert "ix_connection_credentials_user_id" in _index_names(db_path, "connection_credentials")
    # R2（#677）：资源登记与租约表
    assert {"resources", "resource_leases"} <= columns["_tables"]
    assert {
        "owner_id",
        "name",
        "kind",
        "capacity",
        "exclusive",
        "credential_id",
        "config",
    } <= columns["resources"]
    assert {"resource_id", "run_id", "acquired_at", "released_at", "note"} <= columns[
        "resource_leases"
    ]
    assert "ix_resource_leases_run_id" in _index_names(db_path, "resource_leases")
    assert {"project_id", "voyage_id", "credential_id", "report", "metrics"} <= columns[
        "experiments"
    ]
    assert {"seq", "exit_code", "pid", "started_at", "finished_at"} <= columns["experiment_runs"]
    # M5：笔记 / 标签 / 个人状态表（P5b 起笔记/划线归 paper × author，project_id 删列）
    assert {"paper_notes", "paper_tags", "paper_tag_links", "paper_user_meta"} <= columns["_tables"]
    assert {"paper_id", "author_id", "content"} <= columns["paper_notes"]
    assert "project_id" not in columns["paper_notes"]
    # P9e：标签库化——paper_tags 以 library_id 为作用域键（project_id 删列）
    assert {"library_id", "name"} <= columns["paper_tags"]
    assert "project_id" not in columns["paper_tags"]
    assert columns["paper_tag_links"] == {"paper_id", "tag_id"}
    assert {"paper_id", "user_id", "starred", "reading_status"} <= columns["paper_user_meta"]
    # 论文图片：papers.figures JSON 列
    assert "figures" in columns["papers"]
    # M5-A 实验迭代：runs.reflection/primary_value + experiments.figures/iteration_state
    assert {"reflection", "primary_value"} <= columns["experiment_runs"]
    assert {"figures", "iteration_state"} <= columns["experiments"]
    # M5-B 论文撰写：manuscripts 四新列 + manuscript_files 两新列
    assert {"experiment_id", "template", "fact_pack", "latest_compile"} <= columns["manuscripts"]
    assert {"readonly", "updated_by"} <= columns["manuscript_files"]
    # M5-C 论文评审：manuscripts.review_passed
    assert "review_passed" in columns["manuscripts"]
    # idea 2.0：ideas 深耕字段
    assert {"depth", "research_type", "goal", "evidence", "seed_idea_id"} <= columns["ideas"]
    # 文献知识底座：paper_chunks 表
    assert "paper_chunks" in columns["_tables"]
    # 技能系统整体移除（#755）：v1 三张表与课题绑定表都不该再存在
    assert not {"skills", "skill_versions", "user_skills", "project_skills"} & columns["_tables"]
    # 技能市场 S4：skill_listings 表（skill_ratings 已在 P1 去实验室化中移除）；
    # #741 起审核残列已删，「在架」只看 delisted_at
    assert "skill_listings" not in columns["_tables"]
    # #741：跨学科指引搬离 v1，落 guidance_documents
    assert "guidance_documents" in columns["_tables"]
    assert {"slug", "version", "name", "body", "targets", "steps"} <= columns["guidance_documents"]
    # 发表机构列（高级检索）
    assert "affiliations" in columns["papers"]
    # 用户系统 U1：治理列已在 head 删除，只剩头像列
    assert "avatar_path" in columns["users"]
    # 可选全文索引：users.settings 个人设置 JSON 列
    assert "settings" in columns["users"]
    # 任务循环 v1：voyage_runs / voyage_steps 新列
    assert {"mode", "plan_iteration", "done_criteria"} <= columns["voyage_runs"]
    assert {
        "rank",
        "acceptance",
        "requires_gate",
        "budget",
        "attempt",
        "attempts",
        "provenance",
    } <= columns["voyage_steps"]
    # 任务系统库化 P9a：voyage_runs / activities 新增 library_id（project_id 转可空）
    assert "library_id" in columns["voyage_runs"]
    assert "library_id" in columns["activities"]
    # P9b 只剩 submitted_by；status/review_note 审批残留已删（#619）
    assert "submitted_by" in columns["direction_libraries"]
    assert not {"status", "review_note"} & columns["direction_libraries"]
    # 共享开关：direction_libraries.is_public（个人库 / 公开给所有人）
    assert "is_public" in columns["direction_libraries"]
    # P9e：课题 statement 上列；project.definition / projects.ingest_state 退役删列
    assert "statement" in columns["projects"]
    assert "definition" not in columns["projects"]
    assert "ingest_state" not in columns["projects"]
    # 垃圾桶原因等判断字段已迁 library_papers（P4 迁移 B 删列）
    assert "trash_reason" not in columns["papers"]
    assert "status" not in columns["papers"]
    assert "relevance_score" not in columns["papers"]
    assert "wiki_content" not in columns["papers"]
    assert "project_id" not in columns["papers"]
    # PDF 划线标注表（P5b 起无 project_id）
    assert "paper_highlights" in columns["_tables"]
    assert {
        "paper_id",
        "author_id",
        "page",
        "rects",
        "selected_text",
        "color",
        "style",
        "note",
    } <= columns["paper_highlights"]
    assert "project_id" not in columns["paper_highlights"]
    # 稿件文件版本快照表
    assert "manuscript_file_versions" in columns["_tables"]
    assert {"file_id", "seq", "origin", "label", "content"} <= columns["manuscript_file_versions"]
    # 模板库表 + 稿件文件二进制/文件夹列（更早版本，不受本分支往返影响）
    assert "manuscript_templates" in columns["_tables"]
    assert {"key", "name", "source", "scope", "main_tex", "engine"} <= columns[
        "manuscript_templates"
    ]
    assert {"is_binary", "is_folder"} <= columns["manuscript_files"]
    # 用户名列（更早版本）
    assert {"username", "username_locked"} <= columns["users"]
    # llm_providers 模型列表与可选客户端标识
    assert {"models", "user_agent"} <= columns["llm_providers"]
    # llm_call_logs / system_settings 表（更早版本）
    assert {"llm_call_logs", "system_settings"} <= columns["_tables"]
    assert {
        "stage",
        "provider_name",
        "model",
        "duration_ms",
        "status",
        "error",
        "request",
        "response",
        "prompt_tokens",
        "completion_tokens",
        "user_id",
        "project_id",
        "voyage_id",
    } <= columns["llm_call_logs"]
    assert {"key", "value"} <= columns["system_settings"]
    # 反馈改为直开 GitHub issue（#617）：两张反馈表在 head 已删
    assert not {"feedback", "feedback_images"} & columns["_tables"]
    # 个人文献库表（上一版）
    assert "user_library_entries" in columns["_tables"]
    # 作者身份绑定 + 发表记录表 + paper_id 软链列 + per-user LLM 列（上一版）
    assert {"user_author_profiles", "user_publications"} <= columns["_tables"]
    assert "paper_id" in columns["user_publications"]
    assert "owner_id" in columns["llm_providers"]
    assert "owner_id" in columns["model_routes"]
    # 自管轨并入平台配置（#621）：接管开关列已删，providers/routes 的 owner_id 保留
    assert "llm_self_managed" not in columns["users"]
    # 个人库 wiki 快照列已随列卫生退役（#734）
    assert "wiki_content" not in columns["user_library_entries"]
    # 方向文献库两表 + papers.dedup_key（策展人表已在 P1 去实验室化中移除）
    assert {
        "direction_libraries",
        "library_papers",
    } <= columns["_tables"]
    assert "dedup_key" in columns["papers"]
    # 本分支新增：课题「相关研究」书架表（P5a）
    assert "topic_papers" in columns["_tables"]
    assert {
        "topic_id",
        "paper_id",
        "source_library_id",
        "note",
        "added_by",
    } <= columns["topic_papers"]
    # 本分支新增：LLM 用量按方向库归因（P6）
    assert "library_id" in columns["llm_usage"]
    assert "library_id" in columns["llm_call_logs"]
    # 本分支新增：课题 × 文献库关联表（P7 Step 1）
    assert "topic_source_libraries" in columns["_tables"]
    assert columns["topic_source_libraries"] == {"topic_id", "library_id", "created_at"}
    # 本分支新增：书架 / 个人库回收站（软删）
    assert {"trashed_at", "trashed_by"} <= columns["topic_papers"]
    assert "trashed_at" in columns["user_library_entries"]
    # 上一版的两张回滚备份表（策展人回填 / 库任务脱离课题）
    assert "_c5e2a90d_voyage_topic" in columns["_tables"]
    # 本分支新增：个人标签表（paper × user × name，与库标签完全独立）
    assert "user_paper_tags" in columns["_tables"]
    assert {"id", "user_id", "paper_id", "name"} <= columns["user_paper_tags"]
    # 本分支新增：用量面板按时间窗聚合用的 llm_usage.created_at 索引
    assert "ix_llm_usage_created_at" in _index_names(db_path, "llm_usage")
    # 本分支新增：论文级唯一解读表（原列一律保留，只是不再读写）
    assert "paper_wikis" in columns["_tables"]
    assert {"paper_id", "content", "model", "compiled_by"} <= columns["paper_wikis"]
    # 本分支新增：概念统一到论文级（去 library_id，slug 全局唯一）+ 两张回滚留档表
    assert "library_id" not in columns["concepts"]
    assert {"concepts_pre_unify", "paper_concepts_pre_unify"} <= columns["_tables"]
    # 本分支新增：概念转正门槛（candidate / active）
    assert "status" in columns["concepts"]
    # 每用户群机器人配置：token / secret 只存密文，用户 × 平台唯一约束由迁移创建。
    assert "chat_bot_configs" in columns["_tables"]
    assert {
        "user_id",
        "platform",
        "robot_id_encrypted",
        "secret_encrypted",
        "last_delivered_at",
    } <= columns["chat_bot_configs"]
    # 文献库每日简报：结构化统计/论文观察/趋势快照 + 收录或排除理由。
    assert "library_research_digests" in columns["_tables"]
    assert {
        "library_id",
        "voyage_id",
        "report_date",
        "counts",
        "paper_insights",
        "excluded_papers",
        "cross_paper_signals",
        "rolling_trends",
        "trend_content",
    } <= columns["library_research_digests"]
    assert "relevance_reason" in columns["library_papers"]
    assert "scored_run_id" in columns["library_papers"]  # 打分归属改记运行 id
    # 任务对话流：用户与任务 agent 的双向消息
    assert "voyage_messages" in columns["_tables"]

    # 外部 agent 使用有 scope、可撤销、只存摘要的长期凭证。
    assert "integration_tokens" in columns["_tables"]
    assert {
        "user_id",
        "name",
        "token_prefix",
        "token_hash",
        "scopes",
        "expires_at",
        "revoked_at",
        "last_used_at",
    } <= columns["integration_tokens"]
    assert {
        "literature_search_runs",
        "literature_search_hits",
        "literature_source_attempts",
    } <= columns["_tables"]
    assert {
        "library_id",
        "created_by",
        "requested_count",
        "candidate_budget",
        "topic",
        "query_plan",
        "source_config",
        "progress",
    } <= columns["literature_search_runs"]
    assert {
        "run_id",
        "paper_id",
        "source",
        "dedup_key",
        "title",
        "scores",
        "metadata_snapshot",
    } <= columns["literature_search_hits"]
    assert {
        "run_id",
        "source",
        "status",
        "fetched_count",
        "accepted_count",
        "retryable",
    } <= columns["literature_source_attempts"]
    assert {"sha256", "byte_size", "storage_key", "state"} <= columns["pdf_blobs"]
    assert {"paper_id", "blob_id", "source", "sharing_scope", "identity_status"} <= columns[
        "paper_assets"
    ]
    assert {"asset_id", "library_id", "status", "can_read", "can_process"} <= columns[
        "asset_grants"
    ]

    assert {"paper_id", "asset_id", "version_no", "parser", "status", "is_current"} <= columns[
        "paper_content_versions"
    ]
    assert {"content_version_id", "seq", "text", "section_path"} <= columns["paper_content_chunks"]
    assert {"content_version_id", "space", "dim", "embedding"} <= columns[
        "paper_content_version_vectors"
    ]
    assert {"chunk_id", "space", "dim", "embedding"} <= columns["paper_content_chunk_vectors"]

    assert {
        "anchor_type",
        "anchor_key",
        "content_revision",
        "quoted_text",
        "normalized_text",
        "locator",
    } <= columns["paper_evidence_anchors"]

    assert {"download_api_keys", "download_batches", "download_batch_items"} <= columns["_tables"]

    assert {"literature_oa_caches", "literature_oa_attempts"} <= columns["_tables"]

    assert {"primary_domain", "related_domains", "status", "version"} <= columns[
        "interdisciplinary_research_profiles"
    ]
    assert {"library_kind", "interdisciplinary_project_id"} <= columns["direction_libraries"]
    assert "research_mode" in columns["projects"]

    assert {"query_matrix", "evidence_balance"} <= columns["interdisciplinary_research_profiles"]
    assert {
        "profile_id",
        "project_id",
        "version",
        "research_scope",
        "query_matrix",
        "evidence_balance",
    } <= columns["interdisciplinary_research_profile_versions"]

    # 本分支新增：引文边表（#639）
    assert "paper_citations" in columns["_tables"]
    assert {
        "citing_paper_id",
        "cited_paper_id",
        "ref_index",
        "cited_ref_raw",
        "context",
        "intent",
        "confidence",
    } <= columns["paper_citations"]
    assert {
        "ix_paper_citations_citing_paper_id",
        "ix_paper_citations_cited_paper_id",
    } <= _index_names(db_path, "paper_citations")

    # 本分支新增：方法卡双轴向量表（#663）
    assert "method_vectors" in columns["_tables"]
    assert {
        "paper_id",
        "axis",
        "space",
        "dim",
        "embedding",
        "model",
        "text_version",
        "built_at",
        # 向量出自哪张卡（#772）：多学科库并存时算分与显示必须同源
        "schema_id",
    } <= columns["method_vectors"]
    assert "ix_method_vectors_space" in _index_names(db_path, "method_vectors")

    # 结构化抽取产物表（#661）
    assert "paper_extractions" in columns["_tables"]
    assert {
        "paper_id",
        "schema_id",
        "payload",
        "confidence",
        "stage_meta",
        "created_at",
        "updated_at",
    } <= columns["paper_extractions"]
    assert "ix_paper_extractions_paper_id" in _index_names(db_path, "paper_extractions")
    # rerank 端点路径可配（#810）：各家路径不统一，差异存在 provider 上
    assert "rerank_path" in columns["llm_providers"]
    # 按环节的输入预算（#811）：路由上的覆盖值，NULL = 全用默认
    assert "input_budgets" in columns["model_routes"]
    # 每日池条目记来源（#821）：增量同步按库的来源筛池
    assert "sources" in columns["daily_feed_entries"]

    # 账号层已删（#842）：邮箱验证码表不在 head 上
    assert "email_verification_codes" not in columns["_tables"]

    # 先退掉终端日志主键重建：空操作。
    command.downgrade(cfg, "-1")
    version, columns = _inspect_db(db_path)
    assert version == USER_ROWS_MERGE_REVISION

    # 再退掉用户行合并（#850）：空操作。
    command.downgrade(cfg, "-1")
    version, columns = _inspect_db(db_path)
    assert version == SINGLE_USER_REVISION

    # 再退掉单用户化（#842）：acp_agents.shared 建回来。
    command.downgrade(cfg, "-1")
    version, columns = _inspect_db(db_path)
    assert version == NO_ACCOUNTS_REVISION
    assert "shared" in columns["acp_agents"]

    # 再退掉「删验证码表」（#842）：表建回来（空表）。
    command.downgrade(cfg, "-1")
    version, columns = _inspect_db(db_path)
    assert version == ROUTES_TO_AGENTS_REVISION
    assert "email_verification_codes" in columns["_tables"]
    assert "ix_email_verification_codes_email" in _index_names(db_path, "email_verification_codes")

    # 再退掉「路由指向 agent」（#840）：provider_id 回到非空，acp_agent_id 列去掉。
    assert "acp_agent_id" in columns["model_routes"]
    command.downgrade(cfg, "-1")
    version, columns = _inspect_db(db_path)
    assert version == ACP_AGENTS_REVISION
    assert "acp_agent_id" not in columns["model_routes"]

    # 再退掉外部 agent 登记表（#836）。
    command.downgrade(cfg, "-1")
    version, columns = _inspect_db(db_path)
    assert version == LEGACY_PREFS_REVISION
    assert "acp_agents" not in columns["_tables"]

    # 再退掉「删偏好旧行」（只动数据，不动表结构）。
    command.downgrade(cfg, "-1")
    version, columns = _inspect_db(db_path)
    assert version == DAILY_FEED_SOURCES_REVISION

    # 再退掉每日池条目的来源列。
    command.downgrade(cfg, "-1")
    version, columns = _inspect_db(db_path)
    assert version == INPUT_BUDGETS_REVISION
    assert "sources" not in columns["daily_feed_entries"]

    # 再退掉路由的输入预算列。
    command.downgrade(cfg, "-1")
    version, columns = _inspect_db(db_path)
    assert version == RERANK_PATH_REVISION
    assert "input_budgets" not in columns["model_routes"]

    # 再退掉 provider 的 rerank 路径列。
    command.downgrade(cfg, "-1")
    version, columns = _inspect_db(db_path)
    assert version == DAILY_SUBS_REVISION
    assert "rerank_path" not in columns["llm_providers"]

    # 再退掉「每日订阅按人存」（只动数据，不动表结构）。
    command.downgrade(cfg, "-1")
    version, columns = _inspect_db(db_path)
    assert version == SKILLS_DROP_REVISION

    # 再退掉技能表的删除（downgrade 会把空表建回来）。
    command.downgrade(cfg, "-1")
    version, columns = _inspect_db(db_path)
    assert version == VECTOR_SCOPE_REVISION
    assert {"skills", "agent_skills"} <= columns["_tables"]

    # 再退掉方法向量的卡作用域。
    command.downgrade(cfg, "-1")
    version, columns = _inspect_db(db_path)
    assert version == HYPOTHESIS_SEQ_REVISION
    assert "schema_id" not in columns["method_vectors"]

    # 再退掉假设节点的创建序号列。
    command.downgrade(cfg, "-1")
    version, columns = _inspect_db(db_path)
    assert version == MCP_SERVERS_REVISION
    assert "seq" not in columns["hypothesis_nodes"]

    # 再退掉外部 MCP 服务器登记表。
    command.downgrade(cfg, "-1")
    version, columns = _inspect_db(db_path)
    assert version == LIBRARY_DISCIPLINE_REVISION
    assert "mcp_servers" not in columns["_tables"]

    # 再退掉文献库的学科列。
    command.downgrade(cfg, "-1")
    version, columns = _inspect_db(db_path)
    assert version == CRDT_STATE_REVISION
    assert "discipline" not in columns["direction_libraries"]

    # 再退掉协同文档 CRDT 状态列（#347）。
    command.downgrade(cfg, "-1")
    version, columns = _inspect_db(db_path)
    assert version == SKILLS_CONVERGENCE_REVISION
    assert "ydoc_state" not in columns["manuscript_files"]
    assert "content" in columns["manuscript_files"]  # 纯文本投影不受影响

    # 再退掉技能收敛第一步（#741）：审核残列按原形状回来，指引文档表消失。
    command.downgrade(cfg, "-1")
    version, columns = _inspect_db(db_path)
    assert version == HYGIENE_REVISION
    assert "guidance_documents" not in columns["_tables"]
    assert {"status", "decided_by", "comment"} <= columns["skill_listings"]
    assert "delisted_at" not in columns["skill_listings"]
    assert "ix_skill_listings_status" in _index_names(db_path, "skill_listings")

    # 再退掉列卫生（#734）：退役快照列与 created_by 按原形状回来（数据不可恢复，
    # created_by 从 submitted_by 回填——两列写入历史上恒等）。
    command.downgrade(cfg, "-1")
    version, columns = _inspect_db(db_path)
    assert version == SETTINGS_REVISION
    assert {"wiki_content", "compiled_at", "compiled_model"} <= columns["library_papers"]
    assert "wiki_content" in columns["user_library_entries"]
    assert {"wiki_content", "wiki_model"} <= columns["daily_feed_entries"]
    assert {"wiki_snapshot", "snapshot_at"} <= columns["topic_papers"]
    assert "created_by" in columns["direction_libraries"]
    assert "resources" in columns["_tables"]  # 只退一步：资源/租约仍在

    # 再退掉用户偏好搬家（#737）：纯数据迁移，schema 原样。
    command.downgrade(cfg, "-1")
    version, columns = _inspect_db(db_path)
    assert version == RESOURCES_REVISION
    assert "settings" in columns["users"]
    assert "system_settings" in columns["_tables"]

    # 再退掉资源/租约与凭据多态化（#677）：ssh_credentials 按原形状回来。
    command.downgrade(cfg, "-1")
    version, columns = _inspect_db(db_path)
    assert version == METHOD_VECTORS_REVISION
    assert not {"resources", "resource_leases"} & columns["_tables"]
    assert "connection_credentials" not in columns["_tables"]
    assert "ssh_credentials" in columns["_tables"]
    assert not {"kind", "payload_encrypted"} & columns["ssh_credentials"]
    assert "ix_ssh_credentials_user_id" in _index_names(db_path, "ssh_credentials")
    assert "method_vectors" in columns["_tables"]  # 只退一步：方法向量表仍在

    # 再退掉方法向量表（#663）：抽取产物表不受影响。
    command.downgrade(cfg, "-1")
    version, columns = _inspect_db(db_path)
    assert version == EXTRACTIONS_REVISION
    assert "method_vectors" not in columns["_tables"]
    assert "paper_extractions" in columns["_tables"]

    # 再退掉抽取产物表（#661）：引文边表不受影响。
    command.downgrade(cfg, "-1")
    version, columns = _inspect_db(db_path)
    assert version == CITATIONS_REVISION
    assert "paper_extractions" not in columns["_tables"]
    assert "paper_citations" in columns["_tables"]

    # 再退掉引文边表（#639）：假设树表不受影响。
    command.downgrade(cfg, "-1")
    version, columns = _inspect_db(db_path)
    assert version == HYPOTHESIS_TREE_REVISION
    assert "paper_citations" not in columns["_tables"]
    assert "hypothesis_nodes" in columns["_tables"]

    # 再退掉假设树建表（#637）：整表消失，其余不受影响。
    command.downgrade(cfg, "-1")
    version, columns = _inspect_db(db_path)
    assert version == MEMBERS_DROP_REVISION
    assert "hypothesis_nodes" not in columns["_tables"]

    # 再退掉成员表删除（#625）：project_members 按原形状回来，owner 行由
    # projects.owner_id 反向回填（老代码的可见性判据读成员表，没有它课题全隐身）。
    command.downgrade(cfg, "-1")
    version, columns = _inspect_db(db_path)
    assert version == LLM_MERGE_REVISION
    assert {"project_id", "user_id", "role", "created_at", "updated_at"} <= columns[
        "project_members"
    ]
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        with engine.connect() as conn:
            n_projects = conn.execute(text("SELECT COUNT(*) FROM projects")).scalar_one()
            n_members = conn.execute(text("SELECT COUNT(*) FROM project_members")).scalar_one()
            assert n_members == n_projects  # 空库时 0==0；有数据时每课题一行 owner
    finally:
        engine.dispose()

    # 再退掉自管轨合并（#621）：users.llm_self_managed 回来，全员落回默认（被接管）。
    command.downgrade(cfg, "-1")
    version, columns = _inspect_db(db_path)
    assert version == LIBRARY_STATUS_DROP_REVISION
    assert "llm_self_managed" in columns["users"]

    # 再退掉库状态删列：status/review_note 按原形状回来（server_default='active'）。
    command.downgrade(cfg, "-1")
    version, columns = _inspect_db(db_path)
    assert version == FEEDBACK_DROP_REVISION
    assert {"status", "review_note", "submitted_by"} <= columns["direction_libraries"]

    # Then undo the feedback-tables drop: both tables come back.
    command.downgrade(cfg, "-1")
    version, columns = _inspect_db(db_path)
    assert version == GOVERNANCE_DROP_REVISION
    assert {"feedback", "feedback_images"} <= columns["_tables"]
    assert {
        "type",
        "severity",
        "status",
        "module",
        "issue_draft",
        "github_issue_number",
    } <= columns["feedback"]
    assert {"feedback_id", "path", "seq"} <= columns["feedback_images"]

    # Then undo the governance-columns drop: all five columns come back.
    command.downgrade(cfg, "-1")
    version, columns = _inspect_db(db_path)
    assert version == CURATORS_DROP_REVISION
    assert {"role", "read_only", "llm_access", "token_quota", "features"} <= columns["users"]

    # Then undo the curators drop: both tables come back.
    command.downgrade(cfg, "-1")
    version, columns = _inspect_db(db_path)
    assert version == SKILL_RATINGS_DROP_REVISION
    assert "direction_library_curators" in columns["_tables"]
    assert "_pr3_backfilled_curators" in columns["_tables"]

    # Then undo the skill-ratings drop: that table comes back too.
    command.downgrade(cfg, "-1")
    version, columns = _inspect_db(db_path)
    assert version == RANKINGS_DROP_REVISION
    assert "skill_ratings" in columns["_tables"]

    # Then undo the view-events drop: that table comes back too.
    command.downgrade(cfg, "-1")
    version, columns = _inspect_db(db_path)
    assert version == INVITES_DROP_REVISION
    assert "view_events" in columns["_tables"]

    # Then undo the project-invites drop: that table comes back too.
    command.downgrade(cfg, "-1")
    version, columns = _inspect_db(db_path)
    assert version == CODES_DROP_REVISION
    assert "project_invites" in columns["_tables"]

    # Then undo the registration-codes drop: that table comes back too.
    command.downgrade(cfg, "-1")
    version, columns = _inspect_db(db_path)
    assert version == TRANSLATIONS_REVISION
    assert "registration_codes" in columns["_tables"]
    assert "preset_directions" in columns["registration_codes"]

    # Then remove translations and return to the discovery-schedule head.
    command.downgrade(cfg, "-1")
    version, columns = _inspect_db(db_path)
    assert version == DISCOVERY_SCHEDULE_REVISION
    assert "literature_hit_translations" not in columns["_tables"]

    # Then remove schedules and return to the venue-metric head.
    command.downgrade(cfg, "-1")
    version, columns = _inspect_db(db_path)
    assert version == VENUE_METRIC_REVISION
    assert "literature_discovery_schedules" not in columns["_tables"]
    assert not {"trigger", "schedule_version", "scheduled_for"} & columns["literature_search_runs"]

    # Then remove venue metrics and return to immutable scope versions.
    command.downgrade(cfg, "-1")
    version, columns = _inspect_db(db_path)
    assert version == SCOPE_VERSION_REVISION
    assert "literature_venue_metric_cache" not in columns["_tables"]
    assert "venue_metric_snapshot" not in columns["literature_search_hits"]

    # Then remove immutable scope versions and return to the query-matrix head.
    command.downgrade(cfg, "-1")
    version, columns = _inspect_db(db_path)
    assert version == QUERY_MATRIX_REVISION
    assert "interdisciplinary_research_profile_versions" not in columns["_tables"]

    # Then remove the query matrix and return to the profile revision.
    command.downgrade(cfg, "-1")
    version, columns = _inspect_db(db_path)
    assert version == INTERDISCIPLINARY_REVISION
    assert not {"query_matrix", "evidence_balance"} & columns["interdisciplinary_research_profiles"]

    # 再退掉跨学科档案迁移，回到 OA 缓存版本。
    command.downgrade(cfg, "-1")
    version, columns = _inspect_db(db_path)
    assert version == OA_CACHE_REVISION
    assert "interdisciplinary_research_profiles" not in columns["_tables"]
    assert "library_kind" not in columns["direction_libraries"]

    # 再退掉 OA 缓存迁移，回到下载批次协议版本。
    command.downgrade(cfg, "-1")
    version, columns = _inspect_db(db_path)
    assert version == DOWNLOAD_BATCH_REVISION
    assert not {"literature_oa_caches", "literature_oa_attempts"} & columns["_tables"]

    # 再退掉下载批次协议迁移，回到证据锚点版本。
    command.downgrade(cfg, "-1")
    version, columns = _inspect_db(db_path)
    assert version == EVIDENCE_ANCHOR_REVISION
    assert (
        not {
            "download_api_keys",
            "download_batches",
            "download_batch_items",
        }
        & columns["_tables"]
    )

    # 再退掉证据锚点迁移，回到解析内容版本。
    command.downgrade(cfg, "-1")
    version, columns = _inspect_db(db_path)
    assert version == CONTENT_VERSION_REVISION
    assert "paper_evidence_anchors" not in columns["_tables"]

    # 再退掉解析内容版本迁移，回到 PDF 资产版本。
    command.downgrade(cfg, "-1")
    version, columns = _inspect_db(db_path)
    assert version == PDF_ASSET_REVISION
    assert (
        not {
            "paper_content_versions",
            "paper_content_chunks",
            "paper_content_version_vectors",
            "paper_content_chunk_vectors",
        }
        & columns["_tables"]
    )

    # 再退掉 PDF 资产迁移，回到文献发现合同版本。
    command.downgrade(cfg, "-1")
    version, columns = _inspect_db(db_path)
    assert version == LITERATURE_REVISION
    assert not {"pdf_blobs", "paper_assets", "asset_grants"} & columns["_tables"]

    # 再退掉文献发现合同，回到集成令牌版本。
    command.downgrade(cfg, "-1")
    version, columns = _inspect_db(db_path)
    assert version == PREVIOUS_HEAD_REVISION
    assert (
        not {
            "literature_search_runs",
            "literature_search_hits",
            "literature_source_attempts",
        }
        & columns["_tables"]
    )

    # 再退掉集成令牌。
    command.downgrade(cfg, "-1")
    version, columns = _inspect_db(db_path)
    assert version == PROVIDER_UA_REVISION
    assert "integration_tokens" not in columns["_tables"]
    assert "user_agent" in columns["llm_providers"]

    # 再退掉 Provider 级 User-Agent。
    command.downgrade(cfg, "-1")
    version, columns = _inspect_db(db_path)
    assert version == VIEW_EVENTS_REVISION
    assert "user_agent" not in columns["llm_providers"]
    assert "view_events" in columns["_tables"]

    # 再退掉浏览事件。
    command.downgrade(cfg, "-1")
    version, columns = _inspect_db(db_path)
    assert version == VOYAGE_MESSAGES_REVISION
    assert "view_events" not in columns["_tables"]

    # 再退掉任务对话流。
    command.downgrade(cfg, "-1")
    version, columns = _inspect_db(db_path)
    assert version == READ_ONLY_REVISION
    assert "voyage_messages" not in columns["_tables"]

    # 再退掉只读账号。
    command.downgrade(cfg, "-1")
    version, columns = _inspect_db(db_path)
    assert version == MEMORY_KIND_REVISION
    assert "read_only" not in columns["users"]

    # 再退掉记忆分层。
    command.downgrade(cfg, "-1")
    version, columns = _inspect_db(db_path)
    assert version == SKILLS_GLOBAL_REVISION
    assert "kind" not in columns["buddy_memories"]

    # 再退掉技能全局化（user_skills → 空的 project_skills）。
    command.downgrade(cfg, "-1")
    version, columns = _inspect_db(db_path)
    assert version == BUDDY_REVISION
    assert "user_skills" not in columns["_tables"]
    assert "project_skills" in columns["_tables"]
    assert "project_id" in columns["skills"]

    # 再退掉 Buddy 的长期记忆。
    command.downgrade(cfg, "-1")
    version, columns = _inspect_db(db_path)
    assert version == SKILLS_REVISION
    assert "buddy_memories" not in columns["_tables"]

    # 再退掉 Skills v2。
    command.downgrade(cfg, "-1")
    version, columns = _inspect_db(db_path)
    assert version == CONVERSATIONS_REVISION
    assert not {"agent_skills", "agent_skill_files"} & columns["_tables"]

    # 再退掉对话持久化（两张表 + 三处附带列）。
    command.downgrade(cfg, "-1")
    version, columns = _inspect_db(db_path)
    assert version == SCORED_RUN_REVISION
    assert not {"conversations", "conversation_messages"} & columns["_tables"]
    assert "conversation_id" not in columns["llm_usage"]
    assert "context_window" not in columns["model_routes"]

    # 再退掉成员行上的打分任务 id。
    command.downgrade(cfg, "-1")
    version, columns = _inspect_db(db_path)
    assert version == DIGEST_REVISION
    assert "scored_run_id" not in columns["library_papers"]

    # 再退掉每日简报表与相关性理由。
    command.downgrade(cfg, "-1")
    version, columns = _inspect_db(db_path)
    assert version == INLINE_VECTOR_DROP_REVISION
    assert "library_research_digests" not in columns["_tables"]
    assert "relevance_reason" not in columns["library_papers"]

    # 再把主表向量列加回来（数据不搬回，见迁移 docstring）。
    command.downgrade(cfg, "-1")
    version, columns = _inspect_db(db_path)
    assert version == VECTOR_TABLES_REVISION
    assert "embedding" in columns["papers"]
    assert {"embedding_model", "chunk_embedding_model"} <= columns["papers"]
    assert "paper_vectors" in columns["_tables"]  # 只退一步：侧表还在

    # 再退掉三张侧表。
    command.downgrade(cfg, "-1")
    version, columns = _inspect_db(db_path)
    assert version == EFFORT_REVISION
    assert not {"paper_vectors", "paper_chunk_vectors", "idea_vectors"} & columns["_tables"]
    assert "embedding" in columns["ideas"]

    # 再退掉推理档位列。
    command.downgrade(cfg, "-1")
    version, columns = _inspect_db(db_path)
    assert version == CHAT_BOT_REVISION
    assert "effort" not in columns["model_routes"]
    assert {"model", "temperature"} <= columns["model_routes"]  # 同表其余列不受影响
    assert "chat_bot_configs" in columns["_tables"]  # 只退一步：群机器人表仍在

    # 再退掉群机器人配置表。
    command.downgrade(cfg, "-1")
    version, columns = _inspect_db(db_path)
    assert version == CONCEPT_STATUS_REVISION
    assert "chat_bot_configs" not in columns["_tables"]
    assert "status" in columns["concepts"]

    # 再退掉概念状态列。
    command.downgrade(cfg, "-1")
    version, columns = _inspect_db(db_path)
    assert version == INDEX_META_REVISION
    assert "status" not in columns["concepts"]

    # 再退一步：分段来源标记与向量元信息列。
    command.downgrade(cfg, "-1")
    version, columns = _inspect_db(db_path)
    assert version == CONCEPTS_REVISION
    assert "source" not in columns["paper_chunks"]
    assert not {"embedding_model", "embedding_at"} & columns["papers"]
    assert not {"chunk_embedding_model", "chunk_embedding_at"} & columns["papers"]

    # 再退一步落到 a7d0c9e51b34（解读统一）。
    # 概念退回按库分版本：library_id 列回来、留档表清掉、被合并的行与关联还原。
    command.downgrade(cfg, "-1")
    version, columns = _inspect_db(db_path)
    assert version == PREV_REVISION
    assert "library_id" in columns["concepts"]
    assert "concepts_pre_unify" not in columns["_tables"]
    assert "paper_concepts_pre_unify" not in columns["_tables"]
    assert "paper_wikis" in columns["_tables"]  # 只退一步：解读表仍在
    assert "ix_llm_usage_created_at" in _index_names(db_path, "llm_usage")
    # 个人标签表与上一版的两张备份表、回收站列都还在
    assert "user_paper_tags" in columns["_tables"]
    assert {"paper_tags", "paper_tag_links", "_pr3_backfilled_curators"} <= columns["_tables"]
    assert "_c5e2a90d_voyage_topic" in columns["_tables"]
    assert {"trashed_at", "trashed_by"} <= columns["topic_papers"]
    assert "trashed_at" in columns["user_library_entries"]
    assert "email_verification_codes" in columns["_tables"]
    assert "is_public" in columns["direction_libraries"]
    assert "settings" in columns["users"]
    assert "statement" in columns["projects"]
    assert "definition" not in columns["projects"]
    assert "ingest_state" not in columns["projects"]
    # 只退一步：标签库化（mig1）仍在，paper_tags 仍以 library_id 为键
    assert "library_id" in columns["paper_tags"]
    assert "project_id" not in columns["paper_tags"]
    # P9b 三列在场（#619 删列已在链条前段回退），P9a 的 library_id 仍在
    assert {"status", "review_note", "submitted_by"} <= columns["direction_libraries"]
    assert "library_id" in columns["voyage_runs"]
    assert "library_id" in columns["activities"]
    assert "topic_source_libraries" in columns["_tables"]  # P7 表仍在
    assert "library_id" in columns["llm_usage"]
    assert "library_id" in columns["llm_call_logs"]
    # P5b 拆分结构不受影响：笔记/划线仍无 project_id
    assert "project_id" not in columns["paper_notes"]
    assert "project_id" not in columns["paper_highlights"]
    assert "topic_papers" in columns["_tables"]
    # P4 收尾后的内容池结构不受影响：判断列仍只在 library_papers 上
    assert "project_id" not in columns["papers"]
    assert "wiki_content" not in columns["papers"]
    assert "dedup_key" in columns["papers"]
    assert "library_papers" in columns["_tables"]
    assert "wiki_content" in columns["user_library_entries"]
    assert "paper_id" in columns["user_publications"]
    assert "owner_id" in columns["llm_providers"]
    # 上一版仍有的表/列不受影响
    assert "user_library_entries" in columns["_tables"]
    assert {"feedback", "feedback_images"} <= columns["_tables"]
    assert {"llm_call_logs", "system_settings"} <= columns["_tables"]
    assert "models" in columns["llm_providers"]
    assert {"username", "username_locked"} <= columns["users"]
    # 更早的列/表不受影响
    assert {"username", "username_locked"} <= columns["users"]
    assert "manuscript_templates" in columns["_tables"]
    assert {"is_binary", "is_folder"} <= columns["manuscript_files"]
    assert "manuscript_file_versions" in columns["_tables"]
    assert {"avatar_path", "token_quota", "features", "llm_access"} <= columns["users"]
    # 本迁移回退只删 email_verification_codes 表；users.settings 仍在
    assert "settings" in columns["users"]
    assert "project_invites" in columns["_tables"]
    assert "affiliations" in columns["papers"]
    assert {"skill_listings", "skill_ratings"} <= columns["_tables"]
    assert "review_passed" in columns["manuscripts"]
    command.upgrade(cfg, "head")
    version, columns = _inspect_db(db_path)
    assert version == HEAD_REVISION
    # 资源/租约/多态凭据回归（#677）
    assert {"resources", "resource_leases", "connection_credentials"} <= columns["_tables"]
    assert "ssh_credentials" not in columns["_tables"]
    assert "paper_extractions" in columns["_tables"]  # 抽取产物表回归（#661）
    assert "paper_citations" in columns["_tables"]  # 引文边表回归（#639）
    assert "effort" in columns["model_routes"]
    assert "chat_bot_configs" in columns["_tables"]
    # 索引回归；个人标签表、回收站列与两张备份表也仍在
    assert "ix_llm_usage_created_at" in _index_names(db_path, "llm_usage")
    assert "user_paper_tags" in columns["_tables"]
    assert {"id", "user_id", "paper_id", "name"} <= columns["user_paper_tags"]
    assert {"trashed_at", "trashed_by"} <= columns["topic_papers"]
    assert "trashed_at" in columns["user_library_entries"]
    assert "_c5e2a90d_voyage_topic" in columns["_tables"]
    assert "project_id" not in columns["paper_notes"]
    assert "project_id" not in columns["paper_highlights"]
    assert "models" in columns["llm_providers"]
    assert "owner_id" in columns["llm_providers"]
    assert "llm_self_managed" not in columns["users"]  # head 已删（#621）
    # 列卫生在重新 upgrade 后回归（#734）
    assert not {"wiki_content", "compiled_at", "compiled_model"} & columns["library_papers"]
    assert "wiki_content" not in columns["user_library_entries"]
    assert not {"wiki_content", "wiki_model"} & columns["daily_feed_entries"]
    assert not {"wiki_snapshot", "snapshot_at"} & columns["topic_papers"]
    assert "created_by" not in columns["direction_libraries"]
    assert "project_members" not in columns["_tables"]  # head 已删（#625）
    assert "hypothesis_nodes" in columns["_tables"]  # 假设树表回归（#637）
    assert not {"feedback", "feedback_images"} & columns["_tables"]  # head 已删（#617）
    # P9a 列在重新 upgrade 后回归
    assert "library_id" in columns["voyage_runs"]
    assert "library_id" in columns["activities"]
    # P9b 的 submitted_by 回归；status/review_note 在 head 已删（#619）
    assert "submitted_by" in columns["direction_libraries"]
    assert not {"status", "review_note"} & columns["direction_libraries"]
    # P10 归属列在重新 upgrade 后回归
    assert "is_public" in columns["direction_libraries"]
    # P9e 列在重新 upgrade 后回归：projects.statement 在、definition/ingest_state 删
    assert "statement" in columns["projects"]
    assert "definition" not in columns["projects"]
    assert "ingest_state" not in columns["projects"]
    assert "library_id" in columns["paper_tags"]
    assert "project_id" not in columns["paper_tags"]


def test_daily_subscriptions_are_seeded_to_every_user(tmp_path):
    """#806 数据迁移：把 owner 当时的订阅原样播给每个用户。

    读路径改成只认本人的键之后，不播这一份种，升级当天除 owner 外每个人的订阅
    都会变成空——信息流一篇不剩，而他们没做任何操作。
    """
    import json

    db_path = tmp_path / "dailysubs.db"
    cfg = _make_config(db_path)
    command.upgrade(cfg, SKILLS_DROP_REVISION)

    engine = create_engine(f"sqlite:///{db_path}")
    user_cols = (
        "id, email, hashed_password, is_active, is_superuser, is_verified, "
        "display_name, username_locked, settings, created_at, updated_at"
    )
    owner_settings_json = json.dumps(
        {
            "daily.categories": ["cs.AI", "stat.ML"],
            "daily.subscriptions": [{"source": "pubmed", "terms": ["glioma"]}],
            "daily.retention_days": 21,
        }
    )
    with engine.begin() as conn:
        conn.execute(
            text(
                f"INSERT INTO users ({user_cols}) VALUES "
                "('00000000-0000-0000-0000-000000000001', 'owner@e.com', 'x', 1, 0, 1, "
                "'', 0, :settings, '2026-01-01 00:00:00', '2026-01-01 00:00:00')"
            ),
            {"settings": owner_settings_json},
        )
        conn.execute(
            text(
                f"INSERT INTO users ({user_cols}) VALUES "
                "('00000000-0000-0000-0000-000000000002', 'late@e.com', 'x', 1, 0, 1, "
                "'', 0, NULL, '2026-02-01 00:00:00', '2026-02-01 00:00:00')"
            )
        )
        # 自己已经有一份的人不该被覆盖（迁移重跑也必须幂等）
        conn.execute(
            text(
                f"INSERT INTO users ({user_cols}) VALUES "
                "('00000000-0000-0000-0000-000000000003', 'own@e.com', 'x', 1, 0, 1, "
                "'', 0, :settings, '2026-03-01 00:00:00', '2026-03-01 00:00:00')"
            ),
            {"settings": json.dumps({"daily.categories": ["q-bio.NC"]})},
        )

    command.upgrade(cfg, "head")

    def _settings(uid: str) -> dict:
        with engine.begin() as conn:
            raw = conn.execute(
                text("SELECT settings FROM users WHERE id = :i"), {"i": uid}
            ).scalar_one()
        return json.loads(raw) if isinstance(raw, str) else (raw or {})

    late = _settings("00000000-0000-0000-0000-000000000002")
    assert late["daily.categories"] == ["cs.AI", "stat.ML"]
    assert late["daily.subscriptions"] == [{"source": "pubmed", "terms": ["glioma"]}]
    # 部署级的那几项不跟着播：保留天数只有一份真相
    assert "daily.retention_days" not in late

    own = _settings("00000000-0000-0000-0000-000000000003")
    assert own["daily.categories"] == ["q-bio.NC"], "已经有自己那份的不该被覆盖"

    owner = _settings("00000000-0000-0000-0000-000000000001")
    assert owner["daily.categories"] == ["cs.AI", "stat.ML"], "主人那份原样留着"

    command.downgrade(cfg, SKILLS_DROP_REVISION)
    late_after = _settings("00000000-0000-0000-0000-000000000002")
    assert "daily.categories" not in late_after, "播下去的那份该收回"
    own_after = _settings("00000000-0000-0000-0000-000000000003")
    assert own_after["daily.categories"] == ["q-bio.NC"], "用户自己设的不能被一起抹掉"


def test_user_preference_settings_copy_to_owner_and_roundtrip(tmp_path):
    """#737 数据迁移：偏好键拷进 owner（最早活跃用户），downgrade 剔除新键。"""
    import json

    db_path = tmp_path / "prefs.db"
    cfg = _make_config(db_path)
    command.upgrade(cfg, RESOURCES_REVISION)

    engine = create_engine(f"sqlite:///{db_path}")
    user_cols = (
        "id, email, hashed_password, is_active, is_superuser, is_verified, "
        "display_name, username_locked, settings, created_at, updated_at"
    )
    with engine.begin() as conn:
        # 两个用户：owner（更早注册）带既有 settings，晚注册的不该收到拷贝
        conn.execute(
            text(
                f"INSERT INTO users ({user_cols}) VALUES "
                "('00000000-0000-0000-0000-000000000001', 'owner@e.com', 'x', 1, 0, 1, "
                "'', 0, :settings, '2026-01-01 00:00:00', '2026-01-01 00:00:00')"
            ),
            {"settings": json.dumps({"tts": {"enabled": True}})},
        )
        conn.execute(
            text(
                f"INSERT INTO users ({user_cols}) VALUES "
                "('00000000-0000-0000-0000-000000000002', 'late@e.com', 'x', 1, 0, 1, "
                "'', 0, NULL, '2026-02-01 00:00:00', '2026-02-01 00:00:00')"
            )
        )
        for key, value in {
            "daily_feed_categories": ["cs.AI", "stat.ML"],
            "daily_feed_sync_time": "03:45",
            "daily_feed_retention_days": 21,
            "library_sync_scope": "full",
            "tts_config": {"enabled": True, "model": "m"},
            "affiliation_extraction_mode": "on_compile",
            "daily_feed_probe_state": {"date": "2026-09-08"},  # 机器状态，不迁移
        }.items():
            conn.execute(
                text(
                    "INSERT INTO system_settings (key, value, created_at, updated_at) "
                    "VALUES (:k, :v, '2026-01-01 00:00:00', '2026-01-01 00:00:00')"
                ),
                {"k": key, "v": json.dumps(value)},
            )

    # 精确升到 #737 本身（不再用 HEAD：#734 已排在它后面，
    # 用 HEAD 会让下面的 downgrade -1 退错一层）
    command.upgrade(cfg, SETTINGS_REVISION)
    with engine.connect() as conn:
        rows = dict(conn.execute(text("SELECT id, settings FROM users")).fetchall())
        owner = json.loads(rows["00000000-0000-0000-0000-000000000001"])
        assert owner["daily.categories"] == ["cs.AI", "stat.ML"]
        assert owner["daily.sync_time"] == "03:45"
        assert owner["daily.retention_days"] == 21
        assert owner["daily.sync_scope"] == "full"
        assert owner["tts.admin"] == {"enabled": True, "model": "m"}
        assert owner["affiliations.extraction_mode"] == "on_compile"
        assert owner["tts"] == {"enabled": True}  # 既有个人设置原样保留
        assert "daily_feed_probe_state" not in owner  # 机器状态没被误迁
        assert rows["00000000-0000-0000-0000-000000000002"] is None  # 晚注册者不收拷贝
        # 旧行保留（迁移期只读回退的数据源）
        n = conn.execute(
            text("SELECT COUNT(*) FROM system_settings WHERE key = 'daily_feed_categories'")
        ).scalar_one()
        assert n == 1

    command.downgrade(cfg, "-1")
    with engine.connect() as conn:
        raw = conn.execute(
            text("SELECT settings FROM users WHERE id = '00000000-0000-0000-0000-000000000001'")
        ).scalar_one()
        owner = json.loads(raw)
        assert "daily.categories" not in owner and "tts.admin" not in owner
        assert owner["tts"] == {"enabled": True}  # 非命名空间键不受 downgrade 影响
    engine.dispose()


def test_user_preference_migration_skips_empty_deployment(tmp_path):
    """没有用户就没有可归属的人：迁移应静默跳过而不是报错。"""
    db_path = tmp_path / "empty.db"
    cfg = _make_config(db_path)
    command.upgrade(cfg, "head")
    version, _ = _inspect_db(db_path)
    assert version == HEAD_REVISION


def test_schema_hygiene_migration_merges_owner_and_purges_private_llm_rows(tmp_path):
    """#734 的数据面：created_by 并入 submitted_by；自管轨私有 provider/route 行删除。

    在上一版（RESOURCES_REVISION）落一批存量行再升 head：
    - submitted_by 为空但 created_by 有值的库 → 归属回填，不丢归属人；
    - llm_providers / model_routes 的 owner_id 非 NULL 行（#621 只拆了入口没清数据，
      全靠 WHERE owner_id IS NULL 挡着）→ 物理删除；全局行（owner NULL）原样保留。
    """
    db_path = tmp_path / "hygiene.db"
    cfg = _make_config(db_path)
    command.upgrade(cfg, RESOURCES_REVISION)

    owner = "11111111-1111-1111-1111-111111111111"
    engine = create_engine(f"sqlite:///{db_path}")
    now = "2026-09-07 00:00:00"
    with engine.begin() as conn:
        # 归属双列：一行只有 created_by（极老存量），一行两列齐（P9b 后的常态）
        conn.execute(
            text(
                "INSERT INTO direction_libraries "
                "(id, name, library_kind, is_public, created_by, submitted_by, "
                " created_at, updated_at) VALUES "
                "('lib-legacy', '老库', 'standard', 0, :owner, NULL, :now, :now), "
                "('lib-normal', '常态库', 'standard', 0, :owner, :owner, :now, :now)"
            ),
            {"owner": owner, "now": now},
        )
        conn.execute(
            text(
                "INSERT INTO llm_providers "
                "(id, owner_id, name, kind, enabled, created_at, updated_at) VALUES "
                "('prov-global', NULL, 'platform', 'openai_compat', 1, :now, :now), "
                "('prov-private', :owner, 'mine', 'openai_compat', 1, :now, :now)"
            ),
            {"owner": owner, "now": now},
        )
        conn.execute(
            text(
                "INSERT INTO model_routes "
                "(id, owner_id, stage, provider_id, model, created_at, updated_at) VALUES "
                "('route-global', NULL, 'default', 'prov-global', 'gpt-x', :now, :now), "
                "('route-private', :owner, 'default', 'prov-global', 'gpt-x', :now, :now), "
                "('route-dangling', NULL, 'librarian', 'prov-private', 'gpt-x', :now, :now)"
            ),
            {"owner": owner, "now": now},
        )
    engine.dispose()

    command.upgrade(cfg, "head")

    engine = create_engine(f"sqlite:///{db_path}")
    try:
        with engine.connect() as conn:
            merged = dict(
                conn.execute(text("SELECT id, submitted_by FROM direction_libraries")).fetchall()
            )
            # 老存量的归属人回填；常态行不动
            assert merged == {"lib-legacy": owner, "lib-normal": owner}
            providers = {
                r[0] for r in conn.execute(text("SELECT id FROM llm_providers")).fetchall()
            }
            assert providers == {"prov-global"}  # 私有 provider 已清
            routes = {r[0] for r in conn.execute(text("SELECT id FROM model_routes")).fetchall()}
            # 私有路由与挂在私有 provider 下的残路由都清掉，全局行保留
            assert routes == {"route-global"}
    finally:
        engine.dispose()


def test_skill_convergence_data_move_and_roundtrip(tmp_path):
    """#741 数据迁移：指引版本逐行拷进 guidance_documents、v1 行归档、
    listings 的审核状态映射成 delisted_at；downgrade 全部按原形状还原。"""
    import json

    db_path = tmp_path / "skills.db"
    cfg = _make_config(db_path)
    command.upgrade(cfg, SETTINGS_REVISION)

    engine = create_engine(f"sqlite:///{db_path}")
    skill_id = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa01"
    v1_id = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbb01"
    v2_id = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbb02"
    live_id = "cccccccccccccccccccccccccccccc01"
    dead_id = "cccccccccccccccccccccccccccccc02"
    manifest = {"targets": ["forge.generate"], "steps": [{"title": "s", "action": "llm.complete"}]}
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO skills (id, slug, kind, name, scope, is_archived, "
                "created_at, updated_at) VALUES (:id, "
                "'interdisciplinary-research-workflow', 'workflow', '跨学科研究工作流', "
                "'builtin', 0, '2026-01-01 00:00:00', '2026-01-01 00:00:00')"
            ),
            {"id": skill_id},
        )
        for vid, ver, body in ((v1_id, 1, "body one"), (v2_id, 2, "body two")):
            conn.execute(
                text(
                    "INSERT INTO skill_versions (id, skill_id, version, manifest, body, "
                    "created_at, updated_at) VALUES (:id, :sid, :ver, :manifest, :body, "
                    "'2026-01-01 00:00:00', '2026-01-01 00:00:00')"
                ),
                {
                    "id": vid,
                    "sid": skill_id,
                    "ver": ver,
                    "manifest": json.dumps(manifest),
                    "body": body,
                },
            )
        for lid, status in ((live_id, "approved"), (dead_id, "delisted")):
            conn.execute(
                text(
                    "INSERT INTO skill_listings (id, skill_id, skill_version_id, status, "
                    "install_count, created_at, updated_at) VALUES (:id, :sid, :vid, :status, "
                    "0, '2026-01-01 00:00:00', '2026-02-02 00:00:00')"
                ),
                {"id": lid, "sid": skill_id, "vid": v1_id, "status": status},
            )

    # 精确升到 #741 本身（不用 head：#347 已排在它后面，
    # 用 head 会让下面的 downgrade -1 退错一层）
    command.upgrade(cfg, SKILLS_CONVERGENCE_REVISION)
    with engine.connect() as conn:
        docs = conn.execute(
            text(
                "SELECT id, version, name, body, targets, steps FROM guidance_documents "
                "ORDER BY version"
            )
        ).fetchall()
        assert [(d.id, d.version, d.body) for d in docs] == [
            (v1_id, 1, "body one"),  # id 沿用 skill_versions.id（存量 checkpoint 可对回）
            (v2_id, 2, "body two"),
        ]
        assert json.loads(docs[0].targets) == ["forge.generate"]
        assert json.loads(docs[0].steps) == manifest["steps"]
        assert docs[0].name == "跨学科研究工作流"
        # v1 行归档而非删除
        archived = conn.execute(
            text("SELECT is_archived FROM skills WHERE id = :id"), {"id": skill_id}
        ).scalar_one()
        assert archived == 1
        rows = dict(conn.execute(text("SELECT id, delisted_at FROM skill_listings")).fetchall())
        assert rows[live_id] is None  # approved → 在架
        assert rows[dead_id] is not None  # delisted → 下架时间取 updated_at

    command.downgrade(cfg, "-1")
    with engine.connect() as conn:
        assert (
            conn.execute(
                text("SELECT name FROM sqlite_master WHERE name = 'guidance_documents'")
            ).first()
            is None
        )
        archived = conn.execute(
            text("SELECT is_archived FROM skills WHERE id = :id"), {"id": skill_id}
        ).scalar_one()
        assert archived == 0  # 解除归档，v1 行整体还原
        rows = dict(conn.execute(text("SELECT id, status FROM skill_listings")).fetchall())
        assert rows == {live_id: "approved", dead_id: "delisted"}
    engine.dispose()


def test_hypothesis_seq_backfill_follows_the_previous_best_effort_order(tmp_path):
    """回填按 (created_at, id) 编号——那正是改之前的排序键，所以存量数据的读取顺序
    一字不变；而新列让顺序不再依赖墙钟（#784）。

    刻意把第三个节点的 created_at 造成**早于**第二个（时钟回拨的样子）：回填按
    created_at 排，它就该拿到更小的 seq。这条断言钉的是「回填忠实于旧顺序」，
    而不是「回填替旧数据纠错」——存量顺序对不对不是迁移该管的事。
    """
    import json  # noqa: F401 — 与文件内其余用例同款惰性导入

    db_path = tmp_path / "hypseq.db"
    cfg = _make_config(db_path)
    command.upgrade(cfg, MCP_SERVERS_REVISION)

    engine = create_engine(f"sqlite:///{db_path}")
    run_id = "00000000-0000-0000-0000-0000000000aa"
    other_run = "00000000-0000-0000-0000-0000000000bb"
    with engine.begin() as conn:
        for node_id, run, created in [
            ("00000000-0000-0000-0000-000000000001", run_id, "2026-01-01 10:00:00"),
            # 时钟回拨：后插入的行拿到更早的时间戳
            ("00000000-0000-0000-0000-000000000002", run_id, "2026-01-01 10:00:02"),
            ("00000000-0000-0000-0000-000000000003", run_id, "2026-01-01 10:00:01"),
            # 另一个 run：序号按 run 各自从 1 起，不跨 run 连续
            ("00000000-0000-0000-0000-000000000004", other_run, "2026-01-01 09:00:00"),
        ]:
            conn.execute(
                text(
                    "INSERT INTO hypothesis_nodes"
                    " (id, run_id, parent_id, kind, statement, status, created_at, updated_at)"
                    " VALUES (:id, :run, NULL, 'hypothesis', :stmt, 'open', :ts, :ts)"
                ),
                {"id": node_id, "run": run, "stmt": node_id[-1], "ts": created},
            )

    command.upgrade(cfg, "head")
    version, columns = _inspect_db(db_path)
    assert version == HEAD_REVISION
    assert "seq" in columns["hypothesis_nodes"]

    with engine.connect() as conn:
        rows = dict(
            conn.execute(
                text("SELECT id, seq FROM hypothesis_nodes WHERE run_id = :r"),
                {"r": run_id},
            ).fetchall()
        )
    # 按 created_at 升序编号：3 号的时间戳比 2 号早，所以它拿 2、2 号拿 3
    assert rows["00000000-0000-0000-0000-000000000001"] == 1
    assert rows["00000000-0000-0000-0000-000000000003"] == 2
    assert rows["00000000-0000-0000-0000-000000000002"] == 3

    with engine.connect() as conn:
        other = conn.execute(
            text("SELECT seq FROM hypothesis_nodes WHERE run_id = :r"), {"r": other_run}
        ).scalar_one()
    assert other == 1, "序号按 run 各自计数；跨 run 连续会让人误以为两个 run 有关系"

    # 显式指定目标而不是 "-1"：链头之上再加一档时，"-1" 退掉的是那一档，
    # 本用例会莫名其妙地断言失败，而失败信息完全不提真正的原因
    command.downgrade(cfg, MCP_SERVERS_REVISION)
    _version, columns = _inspect_db(db_path)
    assert "seq" not in columns["hypothesis_nodes"]
    # 回退不该带走数据行
    with engine.connect() as conn:
        n = conn.execute(text("SELECT COUNT(*) FROM hypothesis_nodes")).scalar_one()
    assert n == 4
    engine.dispose()


def test_legacy_preference_rows_move_to_the_owner_then_go(tmp_path):
    """#821 E2：删 read_setting 的旧行回退之前，先把旧行里 owner 还没有的值补到 owner
    身上——那正是回退此刻还在读的值，删掉之后行为不变。owner 已有的值不被旧行覆盖。"""
    import json

    db_path = tmp_path / "legacy-prefs.db"
    cfg = _make_config(db_path)
    command.upgrade(cfg, DAILY_FEED_SOURCES_REVISION)

    engine = create_engine(f"sqlite:///{db_path}")
    user_cols = (
        "id, email, hashed_password, is_active, is_superuser, is_verified, "
        "display_name, username_locked, settings, created_at, updated_at"
    )
    with engine.begin() as conn:
        conn.execute(
            text(
                f"INSERT INTO users ({user_cols}) VALUES "
                "('00000000-0000-0000-0000-000000000001', 'owner@e.com', 'x', 1, 0, 1, "
                "'', 0, :settings, '2026-01-01 00:00:00', '2026-01-01 00:00:00')"
            ),
            {"settings": json.dumps({"daily.sync_time": "07:15"})},
        )
        for key, value in (
            ("daily_feed_sync_time", "03:00"),  # owner 已有新键：不覆盖
            ("daily_feed_retention_days", 9),  # owner 没有：补上
            ("tts_config", {"model": "legacy-voice"}),
            ("daily_feed_categories", ["cs.AI"]),  # 只删不搬
            ("literature_search", {"sources": ["pubmed"]}),  # 不相干的行：不动
        ):
            conn.execute(
                text(
                    "INSERT INTO system_settings (key, value, created_at, updated_at) "
                    "VALUES (:k, :v, '2026-01-01', '2026-01-01')"
                ),
                {"k": key, "v": json.dumps(value)},
            )

    command.upgrade(cfg, "head")

    with engine.begin() as conn:
        raw = conn.execute(
            # 按 id 找：7d2e4f9a1b63（#850）把唯一的用户认作本地用户、邮箱已改成 local@
            text("SELECT settings FROM users WHERE id = '00000000-0000-0000-0000-000000000001'")
        ).scalar_one()
        keys = {row[0] for row in conn.execute(text("SELECT key FROM system_settings")).all()}
    settings = json.loads(raw) if isinstance(raw, str) else raw
    assert settings["daily.sync_time"] == "07:15"
    assert settings["daily.retention_days"] == 9
    assert settings["tts.admin"] == {"model": "legacy-voice"}
    assert "daily.categories" not in settings
    assert keys == {"literature_search"}


def test_acp_agents_table_roundtrip(tmp_path):
    """#836：外部 agent 登记表能建能退，退掉不影响上一档。"""
    db_path = tmp_path / "acp.db"
    cfg = _make_config(db_path)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, LEGACY_PREFS_REVISION)
    version, columns = _inspect_db(db_path)
    assert version == LEGACY_PREFS_REVISION
    assert "acp_agents" not in columns["_tables"]
    command.upgrade(cfg, "head")
    version, columns = _inspect_db(db_path)
    assert version == HEAD_REVISION
    assert "permission_policy" in columns["acp_agents"]


def test_single_user_migration_folds_llm_rows_and_drops_agent_sharing(tmp_path):
    """#842 第 7 步：按人配置的 LLM 行并进那一张表，外部 agent 的「共享」列删掉。

    - 只并本地用户自己的那份；别的账号的按人 provider / 路由直接删掉——它们存着别人的
      API key（#850）；
    - model_routes：同一环节已有部署级路由时删掉按人的；没有的改成 NULL；
    - llm_providers：本地用户的改成 NULL，撞名的加「 (2)」；
    - acp_agents.shared：删列；降级建回来、一律 false。
    """
    db_path = tmp_path / "single-user.db"
    cfg = _make_config(db_path)
    command.upgrade(cfg, NO_ACCOUNTS_REVISION)

    local = "00000000000000000000000000000001"
    other = "00000000000000000000000000000002"
    now = "2026-10-01 00:00:00"
    later = "2026-10-02 00:00:00"
    engine = create_engine(f"sqlite:///{db_path}")
    user_cols = (
        "id, email, hashed_password, is_active, is_superuser, is_verified, "
        "display_name, username_locked, created_at, updated_at"
    )
    with engine.begin() as conn:
        # 另一个人比本地用户先建：仍按邮箱认本地用户，不按先后
        conn.execute(
            text(
                f"INSERT INTO users ({user_cols}) VALUES "
                "(:other, 'old@e.com', 'x', 1, 0, 1, '', 0, '2026-01-01', '2026-01-01'), "
                "(:local, 'local@polaris.desktop', 'x', 1, 0, 1, '', 0, '2026-02-01', '2026-02-01')"
            ),
            {"local": local, "other": other},
        )
        conn.execute(
            text(
                "INSERT INTO llm_providers "
                "(id, owner_id, name, kind, enabled, created_at, updated_at) VALUES "
                "('prov-platform', NULL, 'openai', 'openai_compat', 1, :now, :now), "
                "('prov-local', :local, 'openai', 'openai_compat', 1, :now, :now), "
                "('prov-other', :other, 'openai', 'openai_compat', 1, :later, :later), "
                "('prov-unique', :other, 'mine', 'openai_compat', 1, :now, :now)"
            ),
            {"local": local, "other": other, "now": now, "later": later},
        )
        conn.execute(
            text(
                "INSERT INTO model_routes "
                "(id, owner_id, stage, provider_id, model, created_at, updated_at) VALUES "
                "('r-platform-default', NULL, 'default', 'prov-platform', 'a', :now, :now), "
                "('r-local-default', :local, 'default', 'prov-local', 'b', :now, :now), "
                "('r-other-librarian', :other, 'librarian', 'prov-other', 'c', :now, :now), "
                "('r-local-librarian', :local, 'librarian', 'prov-local', 'd', :later, :later), "
                "('r-other-digest', :other, 'digest', 'prov-unique', 'e', :now, :now)"
            ),
            {"local": local, "other": other, "now": now, "later": later},
        )
        conn.execute(
            text(
                "INSERT INTO acp_agents "
                "(id, slug, name, template, command, permission_policy, enabled, shared, "
                " created_at, updated_at) VALUES "
                "('agent-1', 'claude', 'Claude', 'claude', 'claude', 'ask', 1, 1, :now, :now)"
            ),
            {"now": now},
        )
    engine.dispose()

    command.upgrade(cfg, SINGLE_USER_REVISION)
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        with engine.connect() as conn:
            routes = {
                row[0]: (row[1], row[2])
                for row in conn.execute(text("SELECT id, owner_id, stage FROM model_routes"))
            }
            providers = {
                row[0]: (row[1], row[2])
                for row in conn.execute(text("SELECT id, owner_id, name FROM llm_providers"))
            }
    finally:
        engine.dispose()
    # 部署级的 default 留着，本地用户的 default 删掉；librarian 留本地用户那条（改成
    # NULL）；别的账号的 librarian / digest 连同它们的 provider 一起删掉
    assert routes == {
        "r-platform-default": (None, "default"),
        "r-local-librarian": (None, "librarian"),
    }
    # 本地用户的并到 NULL，撞名的加后缀；别的账号的（含 key）删掉
    assert providers == {
        "prov-platform": (None, "openai"),
        "prov-local": (None, "openai (2)"),
    }
    version, columns = _inspect_db(db_path)
    assert version == SINGLE_USER_REVISION
    assert "shared" not in columns["acp_agents"]

    # 降级：shared 列建回来、一律 false；agent 行本身不丢
    command.downgrade(cfg, "-1")
    version, columns = _inspect_db(db_path)
    assert version == NO_ACCOUNTS_REVISION
    assert "shared" in columns["acp_agents"]
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        with engine.connect() as conn:
            rows = conn.execute(text("SELECT slug, shared FROM acp_agents")).all()
    finally:
        engine.dispose()
    assert [(slug, bool(shared)) for slug, shared in rows] == [("claude", False)]


def test_single_user_migration_without_local_user_keeps_the_earliest_users_llm_rows(tmp_path):
    """#850：库里没有 local@ 时，「本地用户」是最早的活跃用户——并它的，删别人的。"""
    db_path = tmp_path / "single-user-no-local.db"
    cfg = _make_config(db_path)
    command.upgrade(cfg, NO_ACCOUNTS_REVISION)
    first = "00000000-0000-0000-0000-000000000001"
    second = "00000000-0000-0000-0000-000000000002"
    engine = create_engine(f"sqlite:///{db_path}")
    with engine.begin() as conn:
        _insert(conn, "users", id=first, email="me@x.com", is_active=1, created_at="2026-01-01")
        _insert(conn, "users", id=second, email="b@x.com", is_active=1, created_at="2026-02-01")
        _insert(conn, "llm_providers", id="p1", owner_id=first, name="mine", kind="openai_compat")
        _insert(
            conn, "llm_providers", id="p2", owner_id=second, name="theirs", kind="openai_compat"
        )
        _insert(conn, "model_routes", id="r1", owner_id=first, stage="default", provider_id="p1")
        _insert(conn, "model_routes", id="r2", owner_id=second, stage="digest", provider_id="p2")
    engine.dispose()

    command.upgrade(cfg, SINGLE_USER_REVISION)
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        with engine.connect() as conn:
            providers = conn.execute(text("SELECT id, owner_id FROM llm_providers")).all()
            routes = conn.execute(text("SELECT id, owner_id FROM model_routes")).all()
    finally:
        engine.dispose()
    assert [tuple(r) for r in providers] == [("p1", None)]
    assert [tuple(r) for r in routes] == [("r1", None)]


# ---------------------------------------------------------------------------
# #850：别的用户行的数据并到本地用户（7d2e4f9a1b63）
# ---------------------------------------------------------------------------


def _filler(col_type: str):
    t = col_type.upper()
    if any(k in t for k in ("BOOL", "INT", "FLOAT", "REAL", "NUMERIC")):
        return 0
    if "DATETIME" in t or "TIMESTAMP" in t:
        return "2026-01-01 00:00:00"
    if "DATE" in t:
        return "2026-01-01"
    if "JSON" in t:
        return "{}"
    return "x"


def _insert(conn, table: str, **values) -> None:
    """插一行：没给的 NOT NULL 列按类型填个占位值（迁移测试不关心它们）。"""
    import json
    import uuid as uuid_mod

    row = dict(values)
    for col in inspect(conn).get_columns(table):
        name = col["name"]
        if name in row:
            continue
        if name == "id":
            row[name] = str(uuid_mod.uuid4())
        elif not col["nullable"] and col.get("default") is None:
            row[name] = _filler(str(col["type"]))
    row = {k: json.dumps(v) if isinstance(v, (dict, list)) else v for k, v in row.items()}
    cols = ", ".join(row)
    params = ", ".join(f":{k}" for k in row)
    conn.execute(text(f"INSERT INTO {table} ({cols}) VALUES ({params})"), row)


def _user_fk_values(conn) -> dict[tuple[str, str], set]:
    """库里每个指向 users.id 的外键列 → 出现过的值。"""
    found = {}
    insp = inspect(conn)
    for table in insp.get_table_names():
        for fk in insp.get_foreign_keys(table):
            if fk["referred_table"] != "users":
                continue
            col = fk["constrained_columns"][0]
            values = {
                r[0] for r in conn.execute(text(f"SELECT {col} FROM {table}")) if r[0] is not None
            }
            if values:
                found[(table, col)] = values
    return found


def test_user_rows_merge_into_the_local_user(tmp_path):
    """老库里有比 local@ 还早注册的账号（和一个停用账号）：所有外键改指本地用户、
    撞唯一约束的留本地用户那行、settings 合并（本地用户的键优先）、别的账号停用。"""
    import json

    db_path = tmp_path / "merge-users.db"
    cfg = _make_config(db_path)
    command.upgrade(cfg, SINGLE_USER_REVISION)

    reg = "00000000-0000-0000-0000-00000000000a"
    local = "00000000-0000-0000-0000-00000000000b"
    stale = "00000000-0000-0000-0000-00000000000c"
    engine = create_engine(f"sqlite:///{db_path}")
    with engine.begin() as conn:
        _insert(
            conn,
            "users",
            id=reg,
            email="me@x.com",
            username="me",
            is_active=1,
            created_at="2026-01-01 00:00:00",
            settings={"daily.sync_time": "07:15", "shared": "reg"},
        )
        _insert(
            conn,
            "users",
            id=stale,
            email="old@x.com",
            username="old",
            is_active=0,
            created_at="2026-01-15 00:00:00",
            settings={"x": 3, "shared": "stale"},
        )
        _insert(
            conn,
            "users",
            id=local,
            email="local@polaris.desktop",
            username="local",
            is_active=1,
            created_at="2026-02-01 00:00:00",
            settings={"shared": "local"},
        )
        # 不撞的行：整行搬过去
        _insert(conn, "projects", id="proj-reg", owner_id=reg, slug="old-proj", name="old")
        _insert(conn, "projects", id="proj-local", owner_id=local, slug="new-proj", name="new")
        _insert(conn, "conversations", id="conv-reg", user_id=reg, scope_kind="global")
        _insert(conn, "connection_credentials", id="cred-reg", user_id=reg, name="gpu", host="h")
        _insert(conn, "paper_notes", id="note-reg", author_id=reg, paper_id="paper-1", content="n")
        _insert(conn, "buddy_memories", id="mem-reg", user_id=reg)
        _insert(conn, "llm_usage", id="use-reg", user_id=reg)
        # 撞唯一约束的：留本地用户那行
        for row_id, uid, name in (
            ("tag-reg-ml", reg, "ml"),
            ("tag-local-ml", local, "ml"),
            ("tag-reg-rl", reg, "rl"),
        ):
            _insert(conn, "user_paper_tags", id=row_id, user_id=uid, paper_id="paper-1", name=name)
        for row_id, uid, paper in (
            ("meta-reg-1", reg, "paper-1"),
            ("meta-local-1", local, "paper-1"),
            ("meta-reg-2", reg, "paper-2"),
        ):
            _insert(conn, "paper_user_meta", id=row_id, user_id=uid, paper_id=paper)
        _insert(conn, "daily_feed_likes", id="like-reg", user_id=reg, entry_id="entry-1")
        _insert(conn, "daily_feed_likes", id="like-local", user_id=local, entry_id="entry-1")
        for row_id, uid, platform in (
            ("bot-reg-f", reg, "feishu"),
            ("bot-local-f", local, "feishu"),
            ("bot-reg-d", reg, "dingtalk"),
        ):
            _insert(conn, "chat_bot_configs", id=row_id, user_id=uid, platform=platform)
        for row_id, uid, key in (
            ("lib-reg-k1", reg, "k1"),
            ("lib-local-k1", local, "k1"),
            ("lib-reg-k2", reg, "k2"),
        ):
            _insert(conn, "user_library_entries", id=row_id, user_id=uid, dedup_key=key)
        # 单列唯一（每人一把）：本地用户没有 → 最早的账号那把搬过来，停用账号那把撞了删掉
        _insert(conn, "download_api_keys", id="key-reg", user_id=reg, key_prefix="pre-reg")
        _insert(conn, "download_api_keys", id="key-stale", user_id=stale, key_prefix="pre-st")
        # 没有外键约束的软引用：人写的评审消息（hex 写法）、闸门的 requested_by
        _insert(conn, "review_sessions", id="rs-1")
        _insert(
            conn,
            "review_messages",
            id="rm-1",
            session_id="rs-1",
            author_type="human",
            author_id=reg.replace("-", ""),
        )
        _insert(conn, "gates", id="gate-1", requested_by=reg, decided_by=reg)
    engine.dispose()

    command.upgrade(cfg, "head")
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        with engine.connect() as conn:
            fk_values = _user_fk_values(conn)
            users = {
                r[0]: (r[1], bool(r[2]), r[3])
                for r in conn.execute(text("SELECT id, email, is_active, settings FROM users"))
            }

            def ids(table):
                return {r[0] for r in conn.execute(text(f"SELECT id FROM {table}"))}

            tags = ids("user_paper_tags")
            metas = ids("paper_user_meta")
            likes = ids("daily_feed_likes")
            bots = ids("chat_bot_configs")
            libs = ids("user_library_entries")
            keys = ids("download_api_keys")
            review_author = conn.execute(text("SELECT author_id FROM review_messages")).scalar()
            gate = conn.execute(text("SELECT requested_by, decided_by FROM gates")).one()
    finally:
        engine.dispose()

    # 每一个指向 users 的外键列里都只剩本地用户
    assert len(fk_values) >= 12, fk_values
    for (table, col), values in fk_values.items():
        assert values == {local}, (table, col, values)
    assert tags == {"tag-local-ml", "tag-reg-rl"}  # 标签取并集
    assert metas == {"meta-local-1", "meta-reg-2"}
    assert likes == {"like-local"}
    assert bots == {"bot-local-f", "bot-reg-d"}
    assert libs == {"lib-local-k1", "lib-reg-k2"}
    assert keys == {"key-reg"}
    assert review_author == local.replace("-", "")
    assert tuple(gate) == (local, local)

    assert users[local][:2] == ("local@polaris.desktop", True)
    assert users[reg][:2] == ("me@x.com", False)
    assert users[stale][:2] == ("old@x.com", False)
    settings = json.loads(users[local][2])
    assert settings == {"shared": "local", "daily.sync_time": "07:15", "x": 3}

    # 降级是空操作；再升一次（幂等）结果不变
    command.downgrade(cfg, SINGLE_USER_REVISION)
    assert _inspect_db(db_path)[0] == SINGLE_USER_REVISION
    command.upgrade(cfg, "head")
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        with engine.connect() as conn:
            assert _user_fk_values(conn) == fk_values
    finally:
        engine.dispose()


def test_user_rows_merge_adopts_the_earliest_user_when_there_is_no_local_user(tmp_path):
    """没有 local@：最早的活跃用户改成 local@（用户名 local 被占就不动），别人的行并给它。"""
    db_path = tmp_path / "merge-no-local.db"
    cfg = _make_config(db_path)
    command.upgrade(cfg, SINGLE_USER_REVISION)

    first = "00000000-0000-0000-0000-000000000001"
    second = "00000000-0000-0000-0000-000000000002"
    engine = create_engine(f"sqlite:///{db_path}")
    with engine.begin() as conn:
        _insert(
            conn,
            "users",
            id=first,
            email="me@x.com",
            username="me",
            is_active=1,
            created_at="2026-01-01 00:00:00",
        )
        _insert(
            conn,
            "users",
            id=second,
            email="b@x.com",
            username="local",
            is_active=1,
            created_at="2026-02-01 00:00:00",
        )
        _insert(conn, "projects", id="proj-1", owner_id=first, slug="a", name="a")
        _insert(conn, "projects", id="proj-2", owner_id=second, slug="b", name="b")
    engine.dispose()

    command.upgrade(cfg, "head")
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        with engine.connect() as conn:
            users = {
                r[0]: (r[1], r[2], bool(r[3]))
                for r in conn.execute(text("SELECT id, email, username, is_active FROM users"))
            }
            owners = {r[0] for r in conn.execute(text("SELECT owner_id FROM projects"))}
    finally:
        engine.dispose()
    assert users[first] == ("local@polaris.desktop", "me", True)
    assert users[second] == ("b@x.com", "local", False)
    assert owners == {first}


def _insert_terminal_log(conn, run_id: str) -> int:
    """不给 id 插一行终端日志，返回库给的 id（SQLite 只有 INTEGER 主键才自动给）。"""
    conn.execute(
        text(
            "INSERT INTO voyage_terminal_logs (run_id, event, message, at) "
            "VALUES (:run_id, 'log', 'hello', '2026-10-09 00:00:00')"
        ),
        {"run_id": run_id},
    )
    return conn.execute(text("SELECT last_insert_rowid()")).scalar_one()


def test_integer_primary_keys_auto_increment_on_a_migrated_sqlite_db(tmp_path):
    """桌面端的库是迁移建的，不是 metadata 建的：迁移出来的自增主键必须声明成 INTEGER。

    SQLite 只有类型恰好是 INTEGER 的单列主键才是 rowid 别名、插入时自动给值；BIGINT
    主键就是普通列，不给 id 的插入全部 NOT NULL 失败（终端日志因此一行都存不下）。
    """
    db_path = tmp_path / "pk.db"
    cfg = _make_config(db_path)
    command.upgrade(cfg, "head")

    engine = create_engine(f"sqlite:///{db_path}")
    try:
        with engine.begin() as conn:
            wrong: dict[str, str] = {}
            tables = conn.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).scalars()
            for table in list(tables):
                pk = [c for c in conn.exec_driver_sql(f"PRAGMA table_info('{table}')") if c[5]]
                declared = (pk[0][2] or "").upper() if len(pk) == 1 else ""
                if "INT" in declared and declared != "INTEGER":
                    wrong[table] = declared
            assert wrong == {}, f"integer primary keys that will not auto-increment: {wrong}"

            run_id = "0" * 32
            assert _insert_terminal_log(conn, run_id) == 1
            assert _insert_terminal_log(conn, run_id) == 2
    finally:
        engine.dispose()


def test_terminal_log_pk_rebuild_keeps_rows_index_and_foreign_key(tmp_path):
    """重建表时已有的终端日志、run_id 索引和指向 voyage_runs 的外键都要留着。"""
    db_path = tmp_path / "pk-keep.db"
    cfg = _make_config(db_path)
    command.upgrade(cfg, USER_ROWS_MERGE_REVISION)

    run_id = "1" * 32
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO voyage_terminal_logs (id, run_id, event, message, at) "
                    "VALUES (7, :run_id, 'llm', 'kept', '2026-10-09 00:00:00')"
                ),
                {"run_id": run_id},
            )
    finally:
        engine.dispose()

    command.upgrade(cfg, TERMINAL_LOG_PK_REVISION)

    engine = create_engine(f"sqlite:///{db_path}")
    try:
        with engine.begin() as conn:
            rows = conn.execute(
                text("SELECT id, run_id, event, message FROM voyage_terminal_logs")
            ).all()
            assert rows == [(7, run_id, "llm", "kept")]
            assert _insert_terminal_log(conn, run_id) == 8
            fks = inspect(conn).get_foreign_keys("voyage_terminal_logs")
            assert [(fk["referred_table"], fk["options"].get("ondelete")) for fk in fks] == [
                ("voyage_runs", "CASCADE")
            ]
    finally:
        engine.dispose()
    assert "ix_voyage_terminal_logs_run_id" in _index_names(db_path, "voyage_terminal_logs")

    # 退回去是空操作，再升上来也不出错
    command.downgrade(cfg, "-1")
    command.upgrade(cfg, "head")
