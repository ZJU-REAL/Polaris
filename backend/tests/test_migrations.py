"""alembic 迁移 sqlite 实跑：全链 upgrade head + 最新 revision（论文评审 M5-C）往返。"""

from pathlib import Path

from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from alembic import command

BACKEND_DIR = Path(__file__).resolve().parent.parent

HEAD_REVISION = "b8c9d0e1f2a3"  # user_system_u1（头像/配额/邀请链接）
PREV_REVISION = "a7b8c9d0e1f2"  # paper_affiliations（发表机构）


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
                    "manuscripts",
                    "manuscript_files",
                    "users",
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

    # 最新 revision 可往返（downgrade 移除用户系统 U1 的列与表）
    command.downgrade(cfg, "-1")
    version, columns = _inspect_db(db_path)
    assert version == PREV_REVISION
    assert "avatar_path" not in columns["users"]
    assert "project_invites" not in columns["_tables"]
    assert "affiliations" in columns["papers"]  # 上一版列不受影响
    assert {"skill_listings", "skill_ratings"} <= columns["_tables"]  # S4 表不受影响
    assert {"skills", "skill_versions", "project_skills"} <= columns["_tables"]  # S1 表不受影响
    assert "review_passed" in columns["manuscripts"]
    # M5-B 列不受影响
    assert {"experiment_id", "template", "fact_pack", "latest_compile"} <= columns["manuscripts"]
    assert {"readonly", "updated_by"} <= columns["manuscript_files"]
    # M5-A 列不受影响
    assert {"reflection", "primary_value"} <= columns["experiment_runs"]
    assert {"figures", "iteration_state"} <= columns["experiments"]
    assert "figures" in columns["papers"]  # 论文图片列不受影响
    # M5 表不受影响
    assert {"paper_notes", "paper_tags", "paper_tag_links", "paper_user_meta"} <= columns["_tables"]
    assert "ssh_credentials" in columns["_tables"]  # M4 表不受影响
    command.upgrade(cfg, "head")
    version, columns = _inspect_db(db_path)
    assert version == HEAD_REVISION
    assert "review_passed" in columns["manuscripts"]
