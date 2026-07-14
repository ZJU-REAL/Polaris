"""alembic 迁移 sqlite 实跑：全链 upgrade head + 最新 revision（embedding 1024）往返。"""

from pathlib import Path

from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from alembic import command

BACKEND_DIR = Path(__file__).resolve().parent.parent

HEAD_REVISION = "d6e7f8a9b0c1"  # bge_m3_embedding_1024
PREV_REVISION = "c4d5e6f7a8b9"  # nullable_route_temperature


def _make_config(db_path: Path) -> Config:
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{db_path}")
    return cfg


def _inspect_db(db_path: Path) -> tuple[str, set[str]]:
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        with engine.connect() as conn:
            version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
            columns = {c["name"] for c in inspect(conn).get_columns("papers")}
    finally:
        engine.dispose()
    return version, columns


def test_migrations_sqlite_upgrade_head_and_roundtrip(tmp_path):
    db_path = tmp_path / "migrate.db"
    cfg = _make_config(db_path)

    command.upgrade(cfg, "head")
    version, columns = _inspect_db(db_path)
    assert version == HEAD_REVISION
    assert "embedding" in columns  # sqlite 分支 JSON variant 列保留

    # 最新 revision 在 sqlite 上升降级均为 no-op，可往返
    command.downgrade(cfg, "-1")
    version, _ = _inspect_db(db_path)
    assert version == PREV_REVISION
    command.upgrade(cfg, "head")
    version, _ = _inspect_db(db_path)
    assert version == HEAD_REVISION
