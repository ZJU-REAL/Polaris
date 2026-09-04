"""alembic 迁移 sqlite 实跑：全链 upgrade head + 最新 revision 往返。"""

from pathlib import Path

from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from alembic import command

BACKEND_DIR = Path(__file__).resolve().parent.parent

HEAD_REVISION = "b0dd1709e2a1"  # Drop project_members (#625)
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
                    "agent_skills",
                    "agent_skill_files",
                    "skills",
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
                    "download_api_keys",
                    "download_batches",
                    "download_batch_items",
                    "literature_oa_caches",
                    "literature_oa_attempts",
                    "interdisciplinary_research_profiles",
                    "interdisciplinary_research_profile_versions",
                    "registration_codes",  # head 已删；仅 downgrade 断言用
                    "project_members",  # head 已删（#625）；仅 downgrade 断言用
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
    # 治理列已随个人化定位删除（#614）
    assert not {"role", "read_only", "llm_access", "token_quota", "features"} & columns[
        "users"
    ]
    # 课题成员表已随个人化定位删除（#625）：归属只看 projects.owner_id
    assert "project_members" not in columns["_tables"]
    assert "owner_id" in columns["projects"]
    assert "effort" in columns["model_routes"]  # 推理档位可配（NULL = 用模型默认）
    # 对话搬到服务端：agent 一轮里可能调好几次工具，历史不能只活在浏览器 localStorage
    assert {"conversations", "conversation_messages"} <= columns["_tables"]
    # Skills v2：技能是「一句 description 常驻 + 正文按需加载」，附件单独一张表
    assert {"agent_skills", "agent_skill_files"} <= columns["_tables"]
    assert {"slug", "description", "body", "allowed_tools", "invocation"} <= columns["agent_skills"]
    assert {"scope_kind", "scope_id", "usage", "active_stream_id"} <= columns["conversations"]
    assert {"blocks", "text", "seq", "status", "sources"} <= columns["conversation_messages"]
    # 这场对话花了多少 token（voyage_id 的对偶）
    assert "conversation_id" in columns["llm_usage"]
    assert "venue_metric_snapshot" in columns["literature_search_hits"]
    assert {"provider", "identity_key", "metrics", "expires_at"} <= columns[
        "literature_venue_metric_cache"
    ]
    assert {"trigger", "schedule_version", "scheduled_for"} <= columns[
        "literature_search_runs"
    ]
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
    # M4：ssh_credentials 表 + experiments / experiment_runs 新列
    assert "ssh_credentials" in columns["_tables"]
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
    # 技能系统 S1 + 技能全局化：skills / skill_versions / user_skills 表（project_skills 已删）
    assert {"skills", "skill_versions", "user_skills"} <= columns["_tables"]
    assert "project_skills" not in columns["_tables"]
    assert "project_id" not in columns["skills"]
    # 技能市场 S4：skill_listings 表（skill_ratings 已在 P1 去实验室化中移除）
    assert "skill_listings" in columns["_tables"]
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
    # 个人库 wiki 快照列（上一版）
    assert "wiki_content" in columns["user_library_entries"]
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
        "wiki_snapshot",
        "snapshot_at",
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
    assert "wiki_content" in columns["library_papers"]  # 存量列保留，可回滚
    assert "wiki_content" in columns["daily_feed_entries"]
    assert "wiki_snapshot" in columns["topic_papers"]
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
    assert {"content_version_id", "seq", "text", "section_path"} <= columns[
        "paper_content_chunks"
    ]
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

    # 先退掉成员表删除（#625）：project_members 按原形状回来，owner 行由
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
    assert not {"trigger", "schedule_version", "scheduled_for"} & columns[
        "literature_search_runs"
    ]

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
    assert not {"query_matrix", "evidence_balance"} & columns[
        "interdisciplinary_research_profiles"
    ]

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
    assert not {
        "download_api_keys",
        "download_batches",
        "download_batch_items",
    } & columns["_tables"]

    # 再退掉证据锚点迁移，回到解析内容版本。
    command.downgrade(cfg, "-1")
    version, columns = _inspect_db(db_path)
    assert version == CONTENT_VERSION_REVISION
    assert "paper_evidence_anchors" not in columns["_tables"]

    # 再退掉解析内容版本迁移，回到 PDF 资产版本。
    command.downgrade(cfg, "-1")
    version, columns = _inspect_db(db_path)
    assert version == PDF_ASSET_REVISION
    assert not {
        "paper_content_versions",
        "paper_content_chunks",
        "paper_content_version_vectors",
        "paper_content_chunk_vectors",
    } & columns["_tables"]

    # 再退掉 PDF 资产迁移，回到文献发现合同版本。
    command.downgrade(cfg, "-1")
    version, columns = _inspect_db(db_path)
    assert version == LITERATURE_REVISION
    assert not {"pdf_blobs", "paper_assets", "asset_grants"} & columns["_tables"]

    # 再退掉文献发现合同，回到集成令牌版本。
    command.downgrade(cfg, "-1")
    version, columns = _inspect_db(db_path)
    assert version == PREVIOUS_HEAD_REVISION
    assert not {
        "literature_search_runs",
        "literature_search_hits",
        "literature_source_attempts",
    } & columns["_tables"]

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
    assert "project_members" not in columns["_tables"]  # head 已删（#625）
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
