"""alembic 迁移 sqlite 实跑：全链 upgrade head + 最新 revision 往返。"""

from pathlib import Path

from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from alembic import command

BACKEND_DIR = Path(__file__).resolve().parent.parent

HEAD_REVISION = "7b3e91c4a2d8"  # Provider 级可选 User-Agent
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
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
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
                    "registration_codes",
                    "feedback",
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
    assert "read_only" in columns["users"]  # 只读账号（游客）
    assert "effort" in columns["model_routes"]  # 推理档位可配（NULL = 用模型默认）
    # 对话搬到服务端：agent 一轮里可能调好几次工具，历史不能只活在浏览器 localStorage
    assert {"conversations", "conversation_messages"} <= columns["_tables"]
    # Skills v2：技能是「一句 description 常驻 + 正文按需加载」，附件单独一张表
    assert {"agent_skills", "agent_skill_files"} <= columns["_tables"]
    assert {"slug", "description", "body", "allowed_tools", "invocation"} <= columns[
        "agent_skills"
    ]
    assert {"scope_kind", "scope_id", "usage", "active_stream_id"} <= columns["conversations"]
    assert {"blocks", "text", "seq", "status", "sources"} <= columns["conversation_messages"]
    # 这场对话花了多少 token（voyage_id 的对偶）
    assert "conversation_id" in columns["llm_usage"]
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
    # 技能市场 S4：skill_listings / skill_ratings 表
    assert {"skill_listings", "skill_ratings"} <= columns["_tables"]
    # 发表机构列（高级检索）
    assert "affiliations" in columns["papers"]
    # 用户系统 U1：users 三新列 + project_invites 表
    assert {"avatar_path", "token_quota", "features", "llm_access"} <= columns["users"]
    assert "project_invites" in columns["_tables"]
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
    # 文献库生命周期 P9b：direction_libraries 新增 status/review_note/submitted_by
    assert {"status", "review_note", "submitted_by"} <= columns["direction_libraries"]
    # 文献库归属 P10：direction_libraries.is_public 个人/公共
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
    # 注册码表（更早版本）
    assert "registration_codes" in columns["_tables"]
    assert {"code", "note", "max_uses", "used_count", "revoked"} <= columns["registration_codes"]
    # 本分支新增：反馈表
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
    # 个人文献库表（上一版）
    assert "user_library_entries" in columns["_tables"]
    # 作者身份绑定 + 发表记录表 + paper_id 软链列 + per-user LLM 列（上一版）
    assert {"user_author_profiles", "user_publications"} <= columns["_tables"]
    assert "paper_id" in columns["user_publications"]
    assert "owner_id" in columns["llm_providers"]
    assert "owner_id" in columns["model_routes"]
    assert "llm_self_managed" in columns["users"]
    # 个人库 wiki 快照列（上一版）
    assert "wiki_content" in columns["user_library_entries"]
    # 注册码预设研究方向列（上一版）
    assert "preset_directions" in columns["registration_codes"]
    # 本分支新增：方向文献库三表 + papers.dedup_key（P4 迁移 A）+ 收尾（迁移 B）
    assert {
        "direction_libraries",
        "direction_library_curators",
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
    assert "_pr3_backfilled_curators" in columns["_tables"]
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

    # 浏览事件：文献库/论文的点击量
    assert "view_events" in columns["_tables"]

    # 最新 revision 可往返：先退掉 Provider 级 User-Agent。
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
    # P9b 三列仍在（本次 downgrade 未触及），P9a 的 library_id 仍在
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
    assert "preset_directions" in columns["registration_codes"]
    assert "wiki_content" in columns["user_library_entries"]
    assert "paper_id" in columns["user_publications"]
    assert "owner_id" in columns["llm_providers"]
    # 上一版仍有的表/列不受影响
    assert "user_library_entries" in columns["_tables"]
    assert {"feedback", "feedback_images"} <= columns["_tables"]
    assert "registration_codes" in columns["_tables"]
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
    assert "_pr3_backfilled_curators" in columns["_tables"]
    assert "_c5e2a90d_voyage_topic" in columns["_tables"]
    assert "project_id" not in columns["paper_notes"]
    assert "project_id" not in columns["paper_highlights"]
    assert "models" in columns["llm_providers"]
    assert "owner_id" in columns["llm_providers"]
    assert "llm_self_managed" in columns["users"]
    assert "registration_codes" in columns["_tables"]
    assert "preset_directions" in columns["registration_codes"]
    assert {"feedback", "feedback_images"} <= columns["_tables"]
    # P9a 列在重新 upgrade 后回归
    assert "library_id" in columns["voyage_runs"]
    assert "library_id" in columns["activities"]
    # P9b 列在重新 upgrade 后回归
    assert {"status", "review_note", "submitted_by"} <= columns["direction_libraries"]
    # P10 归属列在重新 upgrade 后回归
    assert "is_public" in columns["direction_libraries"]
    # P9e 列在重新 upgrade 后回归：projects.statement 在、definition/ingest_state 删
    assert "statement" in columns["projects"]
    assert "definition" not in columns["projects"]
    assert "ingest_state" not in columns["projects"]
    assert "library_id" in columns["paper_tags"]
    assert "project_id" not in columns["paper_tags"]
