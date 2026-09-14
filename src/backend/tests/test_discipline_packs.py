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
from app.services.extraction import schemas as schemas_module
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

    ``_disciplines_loaded`` 一起还原：它是与注册表脱钩的「已装过」标志位，只还注册表
    的话，本文件把学科 schema 摘掉之后标志位仍是 True，惰性加载从此不再重试——后面
    任何一条依赖学科 schema 的用例都会莫名其妙地看不到它们，而且报的是断言失败，
    一点也不像「上一个文件没收拾干净」。
    """
    snapshot = dict(_REGISTRY)
    loaded = schemas_module._disciplines_loaded
    yield
    _REGISTRY.clear()
    _REGISTRY.update(snapshot)
    schemas_module._disciplines_loaded = loaded


def _minimal(name: str = "demo", schema_id: str = "method") -> dict:
    return {
        "name": name,
        "title": "示例学科",
        "schemas": [
            {
                "id": schema_id,
                "prompt": "抽取：\n{fields_spec}\n只输出 JSON。",
                # method 卡必须带两根跨学科的检索轴，否则方法库索引不到
                "fields": [
                    {"name": "purpose", "kind": "text", "max_len": 200},
                    {"name": "mechanism", "kind": "text", "max_len": 200},
                ],
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
    # 用非 method 的 schema，免得撞上「method 必须带两根轴」那条约束
    bad = _minimal(name="entriesbad", schema_id="gaps")
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


def test_discipline_method_card_must_carry_the_retrieval_axes():
    """缺了 purpose/mechanism 不会报错，只会让方法卡检索不到——装了等于没装。"""
    bad = _minimal(name="noaxes")
    bad["schemas"][0]["fields"] = [{"name": "structure", "kind": "text", "max_len": 200}]
    pack = dp.parse_pack(bad, origin="t")
    with pytest.raises(dp.DisciplinePackError) as exc:
        dp.register_pack(pack)
    assert "purpose" in str(exc.value) and "mechanism" in str(exc.value)


def test_non_method_schemas_are_free_to_use_any_fields():
    """只有 method 那条参与方法库；别的 schema 不该被这条约束绑住。"""
    pack = dp.parse_pack(_minimal(name="freeform", schema_id="gaps"), origin="t")
    assert dp.register_pack(pack) == ["freeform.gaps"]


# ---- 对全部内置包生效的检查 ----
#
# 逐个包写死断言的问题是没人会记得回来加：新包只要不解析崩，就悄悄少受一层检查。
# 下面这几条遍历目录，将来加包自动进网。


def _builtin_packs():
    return [
        dp.read_pack_file(path)
        for path in sorted(dp.BUILTIN_DISCIPLINES_DIR.glob("*.yaml"))
    ]


def test_every_builtin_pack_parses():
    packs = _builtin_packs()
    assert len(packs) >= 2, "只有一个包时，这套格式能不能描述别的学科是没被验证过的"


def test_builtin_pack_names_are_unique():
    """包名是 schema 的命名空间前缀，重名等于两个学科的方法卡互相顶掉。"""
    names = [p.name for p in _builtin_packs()]
    assert len(names) == len(set(names)), names


def test_every_builtin_method_card_keeps_the_two_retrieval_axes():
    """缺了这两根轴，那个学科的方法卡抽出来、进了库、却一条都检索不到。"""
    for pack in _builtin_packs():
        for schema in pack.schemas:
            if schema.id != "method":
                continue
            names = {f.name for f in schema.fields}
            assert {"purpose", "mechanism"} <= names, pack.name


def test_no_builtin_pack_ships_the_machine_learning_axes():
    """学科包的意义就是换掉 baseline/dataset 这套口径；带着它们等于白装。"""
    for pack in _builtin_packs():
        for schema in pack.schemas:
            names = {f.name for f in schema.fields}
            assert not ({"baseline", "dataset"} & names), (pack.name, schema.id)


def test_the_packs_actually_differ_from_each_other():
    """两个包除了跨学科的两根轴之外还共用大部分字段的话，这套格式其实只描述了一种学科。"""
    method_fields = {
        pack.name: {f.name for f in schema.fields}
        for pack in _builtin_packs()
        for schema in pack.schemas
        if schema.id == "method"
    }
    axes = {"purpose", "mechanism"}
    domain = {name: fields - axes for name, fields in method_fields.items()}
    seen = list(domain.items())
    for i, (name_a, fields_a) in enumerate(seen):
        for name_b, fields_b in seen[i + 1 :]:
            assert not (fields_a & fields_b), (
                f"{name_a} 与 {name_b} 的领域字段重合：{sorted(fields_a & fields_b)}"
            )


def test_all_builtin_packs_register_side_by_side():
    """四个包同时装上互不干扰，各自的 schema 都带包名前缀进注册表。"""
    dp.load_disciplines()
    registered = {s.id for s in list_schemas()}
    for pack in _builtin_packs():
        for schema in pack.schemas:
            assert f"{pack.name}.{schema.id}" in registered


def test_a_library_only_ever_sees_its_own_packs_schemas():
    """装了四个包，声明某一个学科的库只多拿到那一个的 schema——否则每篇论文都要
    为所有已装学科各付一次 LLM 调用。"""
    dp.load_disciplines()
    packs = _builtin_packs()
    for pack in packs:
        ids = {s.id for s in schemas_for(pack.name)}
        others = [p.name for p in packs if p.name != pack.name]
        for other in others:
            assert not any(i.startswith(f"{other}.") for i in ids), (pack.name, other)


def test_entries_fields_declare_their_entry_keys():
    """entries 字段不声明 entry_keys 会在注册时才炸；包一加进来就该被挡下。"""
    for pack in _builtin_packs():
        for schema in pack.schemas:
            for field in schema.fields:
                if field.kind == "entries":
                    assert field.entry_keys, (pack.name, schema.id, field.name)


# ---- 运行环节继承（#781）----


def test_a_pack_method_card_runs_on_the_method_stage():
    """包里叫 method 的那条干的就是内置 method@1 那件事，该走同一个环节。

    环节是 owner 在设置页逐个配模型的粒度。走 extract_skeleton 的后果不是报错，
    是那批卡悄悄由另一个模型产出——而把 extract_method 指到强模型的人，本意正是
    「方法卡值得好模型」。
    """
    pack = dp.parse_pack(_minimal(name="stageinherit"), origin="t")
    dp.register_pack(pack)
    assert get_schema("stageinherit.method").stage == "extract_method"


def test_a_pack_schema_named_like_another_builtin_inherits_that_one():
    """规则是「同名同工」，不是给 method 开的特例。"""
    pack = dp.parse_pack(_minimal(name="gapsinherit", schema_id="gaps"), origin="t")
    dp.register_pack(pack)
    assert get_schema("gapsinherit.gaps").stage == get_schema("gaps").stage


def test_a_pack_schema_with_no_builtin_namesake_keeps_the_default():
    """内置里没有同名的就没有可继承的口径，回到类缺省。"""
    data = _minimal(name="novel", schema_id="protocol")
    pack = dp.parse_pack(data, origin="t")
    dp.register_pack(pack)
    assert get_schema("novel.protocol").stage == "extract_skeleton"


def test_an_explicit_stage_still_wins():
    """包自己声明了环节就尊重它——包括挂一个自己的 plugin:<包>:<环节> 命名空间。"""
    data = _minimal(name="ownstage")
    data["schemas"][0]["stage"] = "plugin:ownstage:extract"
    pack = dp.parse_pack(data, origin="t")
    dp.register_pack(pack)
    assert get_schema("ownstage.method").stage == "plugin:ownstage:extract"


def test_every_builtin_pack_method_card_routes_like_the_builtin_one():
    """随包目录走，将来加包自动受检。"""
    dp.load_disciplines()
    for pack in _builtin_packs():
        for schema in pack.schemas:
            if schema.id != "method":
                continue
            assert get_schema(f"{pack.name}.method").stage == "extract_method", pack.name
