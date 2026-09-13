"""学科包：用声明式 YAML 往抽取注册表里加学科自己的抽取 schema。

## 为什么要有

`extraction/schemas.py` 的文件头写着「不预埋任何学科内容」，并留了 `register_schema`
这道缝——但在此之前，**全仓只有它自己调用那道缝**，注册三个内置 schema。外部没有
任何安装路径，所以"按学科分化"在设计报告里有、在产品里没有。

具体后果：内置的方法卡 `method@1` 抽的是 purpose / mechanism / baseline / dataset /
protocol——这是机器学习论文的形状。一个做结构工程的人需要的是材料、边界条件、
载荷工况、网格与求解器设置；他今天拿到的方法卡对他没用，而他除了改仓库没有别的办法。

学科包把那道缝接到磁盘上：一个 YAML 声明若干抽取 schema，放进包目录即生效。

## 为什么是声明式 YAML 而不是插件代码

抽取 schema 本身就是**数据**：字段、长度帽、一段 prompt。它不需要执行任何代码。
让它保持数据形态，就能被审阅、被 diff、被随文献库一起导出（file-over-app），
也不必为了加几个字段去承担"在服务端跑第三方代码"的风险——那是插件市场那条
路解决的问题（#754），和这里是两件事。

## 用户目录

除了仓内的内置包，还读 ``<data_dir>/disciplines/*.yaml``。这是 file-over-app
的落点：用户自己写的学科包和他的 PDF、笔记放在一起，复制走就带走了。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.core.config import get_settings
from app.services.extraction.schemas import (
    EntryKey,
    ExtractionSchema,
    SchemaField,
    register_schema,
)

logger = logging.getLogger("polaris.disciplines")

# 刻意不放在 app/packs/ 下：process_packs 用 rglob 递归扫那棵树，任何放进去的
# 非流程包 YAML 都会被它当流程包解析并校验失败。两种包不共享一棵被递归扫描的树。
BUILTIN_DISCIPLINES_DIR = Path(__file__).resolve().parents[1] / "disciplines"

# 字段上限：既防着写坏的包，也防着 prompt 被撑爆——抽取是「整篇正文进、短 JSON 出」，
# 字段太多太长会让模型顾此失彼，抽取质量反而掉。
MAX_FIELDS = 12
MAX_TEXT_LEN = 2000
MAX_ITEMS = 20


class DisciplinePackError(RuntimeError):
    """学科包解析/校验失败。"""


class PackEntryKey(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=64)
    max_len: int = Field(gt=0, le=MAX_TEXT_LEN)
    choices: tuple[str, ...] | None = None


class PackField(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=64)
    kind: Literal["text", "list", "entries"] = "text"
    max_len: int = Field(default=800, gt=0, le=MAX_TEXT_LEN)
    max_items: int | None = Field(default=None, gt=0, le=MAX_ITEMS)
    entry_keys: tuple[PackEntryKey, ...] | None = None

    def to_schema_field(self) -> SchemaField:
        if self.kind == "entries" and not self.entry_keys:
            raise DisciplinePackError(f"字段 {self.name!r} 是 entries，必须声明 entry_keys")
        return SchemaField(
            name=self.name,
            kind=self.kind,
            max_len=self.max_len,
            max_items=self.max_items,
            entry_keys=(
                tuple(
                    EntryKey(name=k.name, max_len=k.max_len, choices=k.choices)
                    for k in self.entry_keys
                )
                if self.entry_keys
                else None
            ),
        )


class PackSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    version: int = Field(default=1, ge=1)
    fields: tuple[PackField, ...] = Field(min_length=1, max_length=MAX_FIELDS)
    prompt: str = Field(min_length=1, max_length=4000)
    #: 不给就让 register_schema 用它的缺省（跟随 extract_skeleton 的路由）
    stage: str | None = None


class DisciplinePack(BaseModel):
    """一个学科包。name 是包标识，schemas 是它带来的抽取 schema。"""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    title: str = Field(min_length=1, max_length=128)
    description: str = Field(default="", max_length=1000)
    schemas: tuple[PackSchema, ...] = Field(min_length=1, max_length=8)


def _to_extraction_schema(pack: DisciplinePack, spec: PackSchema) -> ExtractionSchema:
    """把包里的一条声明变成注册表认识的 ExtractionSchema。

    schema id 强制带包名前缀（``<pack>.<id>``）：不同学科包难免都想叫 ``method``，
    不加前缀迟早互相顶掉——而顶掉的后果是抽取悄悄换了口径，不会报错。
    """
    if "{fields_spec}" not in spec.prompt:
        raise DisciplinePackError(
            f"{pack.name}.{spec.id}：prompt 必须含 {{fields_spec}} 占位符，"
            "字段清单由代码确定性生成，不能手写（否则与 fields 必然漂移）"
        )
    fields = tuple(f.to_schema_field() for f in spec.fields)
    names = [f.name for f in fields]
    if len(set(names)) != len(names):
        raise DisciplinePackError(f"{pack.name}.{spec.id}：字段重名 {names}")

    kwargs: dict[str, Any] = {
        "id": f"{pack.name}.{spec.id}",
        "version": spec.version,
        "fields": fields,
        "prompt_template": spec.prompt,
        # 作用范围：只在声明了该学科的文献库里抽，不进全局遍历
        "pack": pack.name,
    }
    if spec.stage:
        kwargs["stage"] = spec.stage
    return ExtractionSchema(**kwargs)


def parse_pack(data: Any, *, origin: str) -> DisciplinePack:
    if not isinstance(data, dict):
        raise DisciplinePackError(f"学科包 {origin} 顶层必须是映射（mapping）")
    try:
        return DisciplinePack.model_validate(data)
    except ValidationError as exc:
        raise DisciplinePackError(f"学科包 {origin} 校验失败：{exc}") from exc


def read_pack_file(path: Path) -> DisciplinePack:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise DisciplinePackError(f"学科包 {path.name} YAML 解析失败：{exc}") from exc
    return parse_pack(data, origin=path.name)


def user_disciplines_dir() -> Path:
    """用户自己的学科包目录（file-over-app：与 PDF、笔记同处一棵树）。

    与内置目录同名同层级（disciplines/），两处路径形状一致，少一处要记的例外。
    """
    return Path(get_settings().data_dir) / "disciplines"


def pack_dirs() -> list[Path]:
    """内置在前、用户在后：同名时用户包覆盖内置（下面按加载顺序处理）。"""
    return [BUILTIN_DISCIPLINES_DIR, user_disciplines_dir()]


def discover_packs() -> list[DisciplinePack]:
    """扫描全部包目录。单个包坏掉只跳过它自己，不拖垮其余包。"""
    packs: dict[str, DisciplinePack] = {}
    for directory in pack_dirs():
        if not directory.is_dir():
            continue
        for path in sorted(directory.rglob("*.yaml")):
            try:
                pack = read_pack_file(path)
            except DisciplinePackError:
                # 用户手写的包写坏了是常态，不该让整个平台起不来
                logger.warning("跳过无法解析的学科包：%s", path, exc_info=True)
                continue
            packs[pack.name] = pack
    return list(packs.values())


def register_pack(pack: DisciplinePack) -> list[str]:
    """把一个包里的 schema 全部登记，返回登记后的 schema id。"""
    ids: list[str] = []
    for spec in pack.schemas:
        schema = _to_extraction_schema(pack, spec)
        register_schema(schema)
        ids.append(schema.id)
    return ids


def load_disciplines() -> dict[str, list[str]]:
    """启动时调用：发现并登记全部学科包，返回 {包名: [schema id]}。"""
    loaded: dict[str, list[str]] = {}
    for pack in discover_packs():
        try:
            loaded[pack.name] = register_pack(pack)
        except Exception:  # noqa: BLE001 — 一个包登记失败不该拖垮启动
            logger.warning("学科包登记失败：%s", pack.name, exc_info=True)
    if loaded:
        logger.info("学科包已加载：%s", ", ".join(sorted(loaded)))
    return loaded
