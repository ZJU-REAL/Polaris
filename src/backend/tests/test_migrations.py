"""alembic 迁移 sqlite 实跑：全链 upgrade head + 最新 revision（论文评审 M5-C）往返。"""

from pathlib import Path

from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from alembic import command

BACKEND_DIR = Path(__file__).resolve().parent.parent

HEAD_REVISION = "57e55702bcca"  # user_library_entries 表（个人文献库，本分支最新）
PREV_REVISION = "b3f1a7c92e5d"  # feedback + feedback_images 表


def _make_config(db_path: Path) -> Config:
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{db_path}")
    return cfg


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
                    "paper_highlights",
                    "manuscripts",
                    "manuscript_files",
                    "manuscript_file_versions",
                    "manuscript_templates",
                    "users",
                    "voyage_runs",
                    "voyage_steps",
                    "llm_providers",
                    "llm_call_logs",
                    "system_settings",
                    "registration_codes",
                    "feedback",
                    "feedback_images",
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
    assert "embedding" in columns["papers"]  # sqlite 分支 JSON variant 列保留
    # M3 列仍在
    assert {"score_rationale", "matches", "wins", "embedding"} <= columns["ideas"]
    assert "payload" in columns["review_sessions"]
    assert "author_name" in columns["review_messages"]
    assert "agent_persona" not in columns["review_messages"]
    # M4：ssh_credentials 表 + experiments / experiment_runs 新列
    assert "ssh_credentials" in columns["_tables"]
    assert {"project_id", "voyage_id", "credential_id", "report", "metrics"} <= columns[
        "experiments"
    ]
    assert {"seq", "exit_code", "pid", "started_at", "finished_at"} <= columns["experiment_runs"]
    # M5：笔记 / 标签 / 个人状态表
    assert {"paper_notes", "paper_tags", "paper_tag_links", "paper_user_meta"} <= columns["_tables"]
    assert {"paper_id", "project_id", "author_id", "content"} <= columns["paper_notes"]
    assert {"project_id", "name"} <= columns["paper_tags"]
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
    # 技能系统 S1：skills / skill_versions / project_skills 表
    assert {"skills", "skill_versions", "project_skills"} <= columns["_tables"]
    # 技能市场 S4：skill_listings / skill_ratings 表
    assert {"skill_listings", "skill_ratings"} <= columns["_tables"]
    # 发表机构列（高级检索）
    assert "affiliations" in columns["papers"]
    # 用户系统 U1：users 三新列 + project_invites 表
    assert {"avatar_path", "token_quota", "features", "llm_access"} <= columns["users"]
    assert "project_invites" in columns["_tables"]
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
    # 垃圾桶原因标签
    assert "trash_reason" in columns["papers"]
    # PDF 划线标注表（上一版 paper_highlights）
    assert "paper_highlights" in columns["_tables"]
    assert {
        "paper_id",
        "project_id",
        "author_id",
        "page",
        "rects",
        "selected_text",
        "color",
        "style",
        "note",
    } <= columns["paper_highlights"]
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
    # llm_providers.models 列（可用模型列表，更早版本）
    assert "models" in columns["llm_providers"]
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
    # 本分支新增：个人文献库表
    assert "user_library_entries" in columns["_tables"]

    # 最新 revision 可往返（downgrade 移除 user_library_entries 表）
    command.downgrade(cfg, "-1")
    version, columns = _inspect_db(db_path)
    assert version == PREV_REVISION
    assert "user_library_entries" not in columns["_tables"]
    # 上一版仍有的表/列不受影响
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
    assert "project_invites" in columns["_tables"]
    assert "affiliations" in columns["papers"]
    assert {"skill_listings", "skill_ratings"} <= columns["_tables"]
    assert "review_passed" in columns["manuscripts"]
    command.upgrade(cfg, "head")
    version, columns = _inspect_db(db_path)
    assert version == HEAD_REVISION
    assert "models" in columns["llm_providers"]
    assert "registration_codes" in columns["_tables"]
    assert {"feedback", "feedback_images"} <= columns["_tables"]
