"""解读 / 概念统一两条迁移的**数据**搬运（sqlite 实跑 + 往返）。

- 解读：库版多份取 compiled_at 最新的那份，没有库版的退每日推送版，合并成每篇一行；
  原列一律保留，downgrade 删表即回到迁移前。
- 概念：同名多份合并成定义最长的那份，关联改指 + 去重；downgrade 从留档表还原。
"""

import uuid
from datetime import UTC, datetime
from pathlib import Path

from alembic.config import Config
from sqlalchemy import create_engine, text

from alembic import command

BACKEND_DIR = Path(__file__).resolve().parent.parent

WIKI_REVISION = "a7d0c9e51b34"  # 解读统一到 paper_wikis
CONCEPT_REVISION = "b6c2f81d4a09"  # 概念统一到论文级
BEFORE_REVISION = "b7f3d21c8a05"  # 两条迁移之前


def _make_config(db_path: Path) -> Config:
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{db_path}")
    return cfg


def _hex(value: uuid.UUID) -> str:
    """sqlite 上 sa.Uuid 列存 32 位无连字符 hex。"""
    return value.hex


def _seed(db_path: Path) -> dict[str, uuid.UUID]:
    """造存量：3 篇论文 × 库版/每日推送版解读 + 跨库同名概念。"""
    ids = {
        "lib_a": uuid.uuid4(),
        "lib_b": uuid.uuid4(),
        "p_multi": uuid.uuid4(),  # 两个库各编译过一次
        "p_single": uuid.uuid4(),  # 只有一个库编译过
        "p_daily": uuid.uuid4(),  # 只有每日推送编译过
        "p_none": uuid.uuid4(),  # 没编译过
        "c_a": uuid.uuid4(),  # A 库的「大语言模型」
        "c_b": uuid.uuid4(),  # B 库的「大语言模型」（定义更长 → 保留它）
        "c_only": uuid.uuid4(),  # 只有一份的概念
    }
    engine = create_engine(f"sqlite:///{db_path}")
    now = datetime(2026, 7, 1, tzinfo=UTC).isoformat()
    try:
        with engine.begin() as conn:
            for key in ("lib_a", "lib_b"):
                conn.execute(
                    text(
                        "INSERT INTO direction_libraries (id, name, status, is_public,"
                        " created_at, updated_at) VALUES (:id, :name, 'active', 0, :t, :t)"
                    ),
                    {"id": _hex(ids[key]), "name": key, "t": now},
                )
            for key in ("p_multi", "p_single", "p_daily", "p_none"):
                conn.execute(
                    text(
                        "INSERT INTO papers (id, title, created_at, updated_at)"
                        " VALUES (:id, :title, :t, :t)"
                    ),
                    {"id": _hex(ids[key]), "title": key, "t": now},
                )

            def add_member(library: str, paper: str, wiki: str | None, compiled_at: str | None,
                           model: str | None) -> None:
                conn.execute(
                    text(
                        "INSERT INTO library_papers (id, library_id, paper_id, status,"
                        " wiki_content, compiled_at, compiled_model, created_at, updated_at)"
                        " VALUES (:id, :lib, :paper, 'compiled', :wiki, :at, :model, :t, :t)"
                    ),
                    {
                        "id": _hex(uuid.uuid4()),
                        "lib": _hex(ids[library]),
                        "paper": _hex(ids[paper]),
                        "wiki": wiki,
                        "at": compiled_at,
                        "model": model,
                        "t": now,
                    },
                )

            # 同一篇在两个库各编译过一次：取 compiled_at 最新的那份
            add_member("lib_a", "p_multi", "# 旧版解读", "2026-06-01T00:00:00+00:00", "old-model")
            add_member("lib_b", "p_multi", "# 新版解读", "2026-06-03T00:00:00+00:00", "new-model")
            add_member("lib_a", "p_single", "# 单库解读", "2026-06-02T00:00:00+00:00", "m1")
            add_member("lib_a", "p_daily", None, None, None)
            add_member("lib_a", "p_none", None, None, None)

            for paper, wiki, model in (
                ("p_daily", "# 每日推送解读", "daily-model"),
                ("p_multi", "# 每日推送版（应被库版盖过）", "daily-model"),
            ):
                conn.execute(
                    text(
                        "INSERT INTO daily_feed_entries (id, paper_id, feed_date,"
                        " primary_category, categories, announce_type, wiki_content, wiki_model,"
                        " created_at, updated_at)"
                        " VALUES (:id, :paper, '2026-07-01', 'cs.AI', '[\"cs.AI\"]', 'new',"
                        " :wiki, :model, :t, :t)"
                    ),
                    {
                        "id": _hex(uuid.uuid4()),
                        "paper": _hex(ids[paper]),
                        "wiki": wiki,
                        "model": model,
                        "t": now,
                    },
                )

            for key, library, name, slug, definition in (
                ("c_a", "lib_a", "大语言模型", "大语言模型", "短定义"),
                ("c_b", "lib_b", "大语言模型", "大语言模型-x1", "长得多的定义：把边界和用途都写全"),
                ("c_only", "lib_a", "检索增强", "检索增强", "只有一份"),
            ):
                conn.execute(
                    text(
                        "INSERT INTO concepts (id, library_id, name, slug, definition,"
                        " category, created_at, updated_at)"
                        " VALUES (:id, :lib, :name, :slug, :def, 'method', :t, :t)"
                    ),
                    {
                        "id": _hex(ids[key]),
                        "lib": _hex(ids[library]),
                        "name": name,
                        "slug": slug,
                        "def": definition,
                        "t": now,
                    },
                )
            # 同一篇论文同时挂着两份同名概念 → 合并时要去重
            for concept, paper in (
                ("c_a", "p_multi"),
                ("c_b", "p_multi"),
                ("c_b", "p_single"),
                ("c_only", "p_single"),
            ):
                conn.execute(
                    text(
                        "INSERT INTO paper_concepts (paper_id, concept_id)"
                        " VALUES (:paper, :concept)"
                    ),
                    {"paper": _hex(ids[paper]), "concept": _hex(ids[concept])},
                )
    finally:
        engine.dispose()
    return ids


def _rows(db_path: Path, sql: str) -> list[tuple]:
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        with engine.connect() as conn:
            return list(conn.execute(text(sql)).all())
    finally:
        engine.dispose()


def test_wiki_and_concept_unify_migrations_roundtrip(tmp_path):
    db_path = tmp_path / "unify.db"
    cfg = _make_config(db_path)
    command.upgrade(cfg, BEFORE_REVISION)
    ids = _seed(db_path)

    # ---- 解读统一 ----
    command.upgrade(cfg, WIKI_REVISION)
    wikis = {
        row[0]: (row[1], row[2])
        for row in _rows(db_path, "SELECT paper_id, content, model FROM paper_wikis")
    }
    assert len(wikis) == 3  # 4 篇里 3 篇有解读，每篇一行
    assert wikis[_hex(ids["p_multi"])] == ("# 新版解读", "new-model")  # 多份取最新
    assert wikis[_hex(ids["p_single"])] == ("# 单库解读", "m1")
    assert wikis[_hex(ids["p_daily"])] == ("# 每日推送解读", "daily-model")  # 库版没有才用它
    assert _hex(ids["p_none"]) not in wikis
    # 编译时间沿用原 compiled_at（编译徽标不失真）
    stamp = _rows(
        db_path,
        f"SELECT updated_at FROM paper_wikis WHERE paper_id = '{_hex(ids['p_multi'])}'",
    )[0][0]
    assert str(stamp).startswith("2026-06-03")
    # 原列一律没动
    assert len(_rows(db_path, "SELECT id FROM library_papers WHERE wiki_content IS NOT NULL")) == 3
    assert (
        len(_rows(db_path, "SELECT id FROM daily_feed_entries WHERE wiki_content IS NOT NULL")) == 2
    )

    # ---- 概念统一 ----
    command.upgrade(cfg, CONCEPT_REVISION)
    concepts = _rows(db_path, "SELECT id, name, slug, definition FROM concepts ORDER BY name")
    assert len(concepts) == 2  # 同名两份合并成一份
    kept = next(row for row in concepts if row[1] == "大语言模型")
    assert kept[0] == _hex(ids["c_b"])  # 定义最长的那份留下
    assert kept[3].startswith("长得多的定义")
    links = {
        (row[0], row[1])
        for row in _rows(db_path, "SELECT paper_id, concept_id FROM paper_concepts")
    }
    # c_a 的关联改指到 c_b；p_multi 上两份同名概念去重成一条
    assert links == {
        (_hex(ids["p_multi"]), _hex(ids["c_b"])),
        (_hex(ids["p_single"]), _hex(ids["c_b"])),
        (_hex(ids["p_single"]), _hex(ids["c_only"])),
    }
    assert len(_rows(db_path, "SELECT id FROM concepts_pre_unify")) == 3  # 全量留档

    # ---- 往返：概念回到按库分版本，解读表删掉后存量仍在原列 ----
    command.downgrade(cfg, WIKI_REVISION)
    concepts = _rows(db_path, "SELECT id, library_id, name, slug FROM concepts ORDER BY id")
    assert len(concepts) == 3  # 被合并掉的那份复活
    assert {row[1] for row in concepts} == {_hex(ids["lib_a"]), _hex(ids["lib_b"])}
    links = {
        (row[0], row[1])
        for row in _rows(db_path, "SELECT paper_id, concept_id FROM paper_concepts")
    }
    assert links == {
        (_hex(ids["p_multi"]), _hex(ids["c_a"])),
        (_hex(ids["p_multi"]), _hex(ids["c_b"])),
        (_hex(ids["p_single"]), _hex(ids["c_b"])),
        (_hex(ids["p_single"]), _hex(ids["c_only"])),
    }

    command.downgrade(cfg, BEFORE_REVISION)
    tables = {row[0] for row in _rows(db_path, "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "paper_wikis" not in tables
    # 存量解读一直在原列上，回滚后照样读得到
    assert len(_rows(db_path, "SELECT id FROM library_papers WHERE wiki_content IS NOT NULL")) == 3
