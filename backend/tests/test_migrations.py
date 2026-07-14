"""alembic 迁移 sqlite 实跑：全链 upgrade head + 最新 revision（M3 idea/review 列）往返。"""

from pathlib import Path

from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from alembic import command

BACKEND_DIR = Path(__file__).resolve().parent.parent

HEAD_REVISION = "e8f9a0b1c2d3"  # idea_forge_review_m3
PREV_REVISION = "d6e7f8a9b0c1"  # bge_m3_embedding_1024


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
            columns = {
                table: {c["name"] for c in inspector.get_columns(table)}
                for table in ("papers", "ideas", "review_sessions", "review_messages")
            }
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
    # M3 新增列
    assert {"score_rationale", "matches", "wins", "embedding"} <= columns["ideas"]
    assert "payload" in columns["review_sessions"]
    assert "author_name" in columns["review_messages"]
    assert "agent_persona" not in columns["review_messages"]

    # 最新 revision 可往返（downgrade 移除 M3 列、恢复 agent_persona）
    command.downgrade(cfg, "-1")
    version, columns = _inspect_db(db_path)
    assert version == PREV_REVISION
    assert "score_rationale" not in columns["ideas"]
    assert "agent_persona" in columns["review_messages"]
    command.upgrade(cfg, "head")
    version, columns = _inspect_db(db_path)
    assert version == HEAD_REVISION
    assert "matches" in columns["ideas"]
