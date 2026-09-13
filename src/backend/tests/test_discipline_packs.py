"""学科包：把抽取注册表那道缝接到磁盘上。

审计出的问题是具体的——`register_schema` 全仓只有 extraction/schemas.py 自己调用，
所以"抽取 schema 按学科分化"在设计里有、在产品里没有。一个做结构的人拿到的方法卡
是机器学习口径（baseline / dataset），而他除了改仓库没有别的办法。

这些用例钉的就是那条路：内置学科包能登记、用户目录里的包能登记、坏包不拖垮别人、
schema id 强制带包名前缀。
"""

from pathlib import Path

import pytest
import yaml

from app.services import discipline_packs as dp
from app.services.extraction.schemas import (
    _REGISTRY,
    get_schema,
    list_schemas,
    schemas_for,
)

CIVIL = dp.BUILTIN_DISCIPLINES_DIR / "structural-engineering.yaml"


@pytest.fixture(autouse=True)
def _restore_schema_registry():
    """抽取注册表是模块级全局：用例注册的 schema 必须还回去。

    不还的后果很具体——test_paper_extraction 断言 enrich 抽出的 schema 集合，
    本文件泄漏进去的学科 schema 会让那条用例莫名多出一行。
    """
    snapshot = dict(_REGISTRY)
    yield
    _REGISTRY.clear()
    _REGISTRY.update(snapshot)


def _minimal(name: str = "demo", schema_id: str = "method") -> dict:
    return {
        "name": name,
        "title": "示例学科",
        "schemas": [
            {
                "id": schema_id,
                "prompt": "抽取：\n{fields_spec}\n只输出 JSON。",
                "fields": [{"name": "purpose", "kind": "text", "max_len": 200}],
            }
        ],
    }


def test_builtin_structural_pack_parses():
    pack = dp.read_pack_file(CIVIL)
    assert pack.name == "structural"
    ids = {s.id for s in pack.schemas}
    assert "method" in ids


def test_structural_method_card_uses_domain_axes_not_ml_ones():
    """这条是整件事的意义所在：非 CS 学科拿到自己的抽取口径。"""
    pack = dp.read_pack_file(CIVIL)
    method = next(s for s in pack.schemas if s.id == "method")
    names = {f.name for f in method.fields}
    # 结构工程要的轴
    assert {"structure", "actions", "analysis", "validation"} <= names
    # 机器学习的轴不该出现在结构方法卡里
    assert "dataset" not in names
    assert "baseline" not in names
    # purpose / mechanism 跨学科保留：方法库的「同目的异机制」检索靠这两根轴
    assert {"purpose", "mechanism"} <= names


def test_registered_schema_id_is_namespaced_by_pack():
    pack = dp.parse_pack(_minimal(name="structural2"), origin="t")
    ids = dp.register_pack(pack)
    # 不同学科包都想叫 method；不加前缀会互相顶掉，而顶掉不会报错，
    # 只会让抽取悄悄换了口径
    assert ids == ["structural2.method"]
    # get_schema 未知 id 直接抛，所以取到即证明登记成功
    assert get_schema("structural2.method").fields[0].name == "purpose"
    assert "structural2.method" in {s.id for s in list_schemas()}


def test_prompt_must_carry_the_fields_placeholder():
    bad = _minimal(name="nofields")
    bad["schemas"][0]["prompt"] = "手写字段清单：purpose"
    pack = dp.parse_pack(bad, origin="t")
    with pytest.raises(dp.DisciplinePackError) as exc:
        dp.register_pack(pack)
    # 字段清单必须由代码生成，否则 prompt 与 fields 必然漂移
    assert "fields_spec" in str(exc.value)


def test_duplicate_field_names_are_rejected():
    bad = _minimal(name="dupe")
    bad["schemas"][0]["fields"].append({"name": "purpose", "kind": "text", "max_len": 100})
    pack = dp.parse_pack(bad, origin="t")
    with pytest.raises(dp.DisciplinePackError):
        dp.register_pack(pack)


def test_entries_field_requires_entry_keys():
    bad = _minimal(name="entriesbad")
    bad["schemas"][0]["fields"] = [{"name": "items", "kind": "entries", "max_items": 3}]
    pack = dp.parse_pack(bad, origin="t")
    with pytest.raises(dp.DisciplinePackError):
        dp.register_pack(pack)


def test_unknown_keys_are_rejected_rather_than_ignored():
    bad = _minimal(name="typo")
    bad["schemas"][0]["promt"] = "typo key"  # 拼错的键
    with pytest.raises(dp.DisciplinePackError):
        dp.parse_pack(bad, origin="t")


def test_user_directory_packs_are_discovered(tmp_path, monkeypatch):
    """file-over-app：用户自己写的学科包和他的 PDF、笔记放在一起。"""
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "data_dir", str(tmp_path), raising=False)
    user_dir = tmp_path / "disciplines"
    user_dir.mkdir(parents=True)
    (user_dir / "mine.yaml").write_text(
        yaml.safe_dump(_minimal(name="mine"), allow_unicode=True), encoding="utf-8"
    )

    names = {p.name for p in dp.discover_packs()}
    assert "mine" in names
    # 内置包仍在：用户目录是追加，不是替换
    assert "structural" in names


def test_a_broken_pack_does_not_stop_the_others(tmp_path, monkeypatch):
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "data_dir", str(tmp_path), raising=False)
    user_dir = tmp_path / "disciplines"
    user_dir.mkdir(parents=True)
    (user_dir / "broken.yaml").write_text("name: [this is not a mapping", encoding="utf-8")
    (user_dir / "good.yaml").write_text(
        yaml.safe_dump(_minimal(name="good"), allow_unicode=True), encoding="utf-8"
    )

    names = {p.name for p in dp.discover_packs()}
    # 手写的包写坏了是常态，不该让平台起不来
    assert "good" in names
    assert "broken" not in names


def test_load_disciplines_registers_the_builtin_pack():
    loaded = dp.load_disciplines()
    assert "structural" in loaded
    assert "structural.method" in loaded["structural"]
    schema = get_schema("structural.method")
    # 字段清单进 prompt 是确定性生成的，与 fields 永不漂移
    rendered = schema.system_prompt()
    assert "actions" in rendered and "validation" in rendered


def test_pack_dirs_put_user_after_builtin():
    dirs = dp.pack_dirs()
    assert dirs[0] == dp.BUILTIN_DISCIPLINES_DIR
    assert dirs[-1] == dp.user_disciplines_dir()
    assert isinstance(dirs[-1], Path)


def test_discipline_schema_does_not_leak_into_other_libraries():
    """作用范围是这件事的要害：装一个学科包，不该让别的学科的论文也多抽一遍。"""
    dp.register_pack(dp.parse_pack(_minimal(name="geo"), origin="t"))

    builtin_ids = {s.id for s in schemas_for(None)}
    # 没声明学科的库：只有跨学科通用的内置 schema
    assert "geo.method" not in builtin_ids
    assert {"skeleton", "method", "gaps"} <= builtin_ids

    scoped_ids = {s.id for s in schemas_for("geo")}
    # 声明了该学科的库：内置 + 该学科的
    assert "geo.method" in scoped_ids
    assert {"skeleton", "method", "gaps"} <= scoped_ids

    # 声明了别的学科：拿不到 geo 的
    assert "geo.method" not in {s.id for s in schemas_for("structural")}


def test_builtin_schemas_carry_no_pack():
    for schema in schemas_for(None):
        assert schema.pack is None
