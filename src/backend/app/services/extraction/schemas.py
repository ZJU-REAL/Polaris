"""抽取 schema 注册表（#661）。

schema 决定三件事：抽哪些字段（白名单）、每个字段长什么样（text/list + 长度帽）、
怎么向模型要（prompt 模板）。运行时（runtime.py）对着 schema 做归一化——模型输出
里不在白名单的键直接丢弃，超长截断，空串置空。

内置两个通用 schema：骨架 skeleton@1（问题-方法-发现-局限，ORKG contribution
思路）、方法卡 method@1（目的-机制-基线-数据集-流程，#663 方法库的原料，
purpose 与 mechanism 各自建向量做双轴检索，见 services/method_index.py）与
缺口台账 gaps@1（GAPMAP 式条目列表，#665）。学科 schema（PICO /
任务-数据集-指标 / 合成配方…）按设计报告属后续批次，本模块只留
``register_schema`` 这道缝，不预埋任何学科内容。

版本语义：字段集合或含义变了就 +1 并落进 stage_meta.version，读方据此判断
存量产物是否过期。同一 id 只注册最新版——不做多版本共存，个人平台的量级
重抽一遍比维护版本矩阵便宜。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class EntryKey:
    """entries 字段里单个条目的一个键：白名单 + 长度帽 + 可选枚举。

    所有声明的键都是必填——entries 型字段的价值在于每条都是完整的结构化记录
    （比如缺口台账的每条必须带原文出处），缺任何一键整条丢弃，不做半条记录。
    """

    name: str
    max_len: int
    # 取值枚举（如缺口条目的 kind）：归一化时先 strip+小写再比对，枚举外整条丢弃
    choices: tuple[str, ...] | None = None


@dataclass(frozen=True, slots=True)
class SchemaField:
    """一个抽取字段：text = 单段文本；list = 字符串列表（去重去空、条数封顶）；
    entries = 结构化条目列表（每条是白名单键的 dict，见 EntryKey）。"""

    name: str
    kind: str  # "text" | "list" | "entries"
    max_len: int  # text：整段字符帽；list：单条字符帽；entries：不使用（帽在 EntryKey 上）
    max_items: int | None = None  # list / entries：最多保留几条
    # 仅 entries：条目键的白名单规格；模型输出里不在其中的键直接丢弃
    entry_keys: tuple[EntryKey, ...] | None = None


@dataclass(frozen=True, slots=True)
class ExtractionSchema:
    id: str
    version: int
    fields: tuple[SchemaField, ...]
    # system prompt 模板，{fields_spec} 占位符由 field_spec_text() 填充
    prompt_template: str
    # 该 schema 走哪个 LLM 环节（须在 core/llm/router.py 的 STAGES 里注册好路由与档位；
    # 学科包挂新 schema 时要么复用已有环节，要么连同 STAGES/前端清单一起加）
    stage: str = "extract_skeleton"

    def field_spec_text(self) -> str:
        """字段清单的自然语言描述（进 prompt，确定性生成，与 fields 永不漂移）。"""
        lines = []
        for f in self.fields:
            if f.kind == "entries":
                keys = []
                for k in f.entry_keys or ():
                    if k.choices:
                        keys.append(f'"{k.name}"（取值限 {" / ".join(k.choices)}）')
                    else:
                        keys.append(f'"{k.name}"（不超过 {k.max_len} 字）')
                lines.append(
                    f'- "{f.name}"：对象数组，最多 {f.max_items} 条，'
                    f"每条含且仅含键 {'、'.join(keys)}；没有可靠内容就给空数组"
                )
            elif f.kind == "list":
                lines.append(
                    f'- "{f.name}"：字符串数组，最多 {f.max_items} 条，'
                    f"每条不超过 {f.max_len} 字；没有可靠内容就给空数组"
                )
            else:
                lines.append(
                    f'- "{f.name}"：一段文本，不超过 {f.max_len} 字；'
                    "原文没讲清就给 null，不要编造"
                )
        return "\n".join(lines)

    def system_prompt(self) -> str:
        return self.prompt_template.format(fields_spec=self.field_spec_text())


# 通用骨架：任何学科的论文都答得上的四个问题（问题-方法-发现-局限）。
# prompt 首行的 POLARIS_EXTRACT_SKELETON 是 fake provider 的识别标记
# （core/llm/fake.py 对齐），真模型把它当无害前缀。
SKELETON_SCHEMA = ExtractionSchema(
    id="skeleton",
    version=1,
    fields=(
        SchemaField("problem", "text", max_len=800),
        SchemaField("method", "text", max_len=800),
        SchemaField("findings", "list", max_len=300, max_items=5),
        SchemaField("limitations", "list", max_len=300, max_items=3),
    ),
    prompt_template=(
        "POLARIS_EXTRACT_SKELETON\n"
        "你是论文结构化抽取器。根据给定论文的标题与正文，抽取它的通用骨架，"
        "全部字段用中文表述（专有名词保留原文）：\n"
        "{fields_spec}\n"
        '只输出一个 JSON 对象，键为上述字段名，另加 "confidence"（0 到 1，'
        "你对整份抽取的把握）。只依据原文，不引入外部知识。"
    ),
)

# 方法卡（#663）：把一篇论文的做法拆成「要达成什么（purpose）」与「靠什么机制
# 达成（mechanism）」两根轴，外加复现要素（基线/数据集/流程）。拆成两根轴不是
# 分类洁癖——方法库的「同目的异机制」类比检索靠它：purpose 轴找目的相近的论文，
# 再按 mechanism 轴的距离把「换了个思路」的排到前面。字段短帽（400/600 字）是
# 有意的：进向量的文本要一句话说清一根轴，长篇大论会把两根轴搅成一团。
# prompt 首行的 POLARIS_EXTRACT_METHOD 是 fake provider 的识别标记（core/llm/fake.py）。
METHOD_SCHEMA = ExtractionSchema(
    id="method",
    version=1,
    fields=(
        SchemaField("purpose", "text", max_len=400),
        SchemaField("mechanism", "text", max_len=400),
        SchemaField("baseline", "list", max_len=200, max_items=5),
        SchemaField("dataset", "list", max_len=200, max_items=5),
        SchemaField("protocol", "text", max_len=600),
    ),
    prompt_template=(
        "POLARIS_EXTRACT_METHOD\n"
        "你是论文方法卡抽取器。根据给定论文的标题与正文，把它的做法拆成方法卡，"
        "全部字段用中文表述（专有名词保留原文）：\n"
        "{fields_spec}\n"
        "其中 \"purpose\" 只写这个方法要达成的目标（不写怎么做），"
        "\"mechanism\" 只写达成目标的核心机制（不复述目标）；"
        "\"baseline\" 是对比的基线方法名，\"dataset\" 是用到的数据集名，"
        "\"protocol\" 是实验流程的一段概述。"
        '只输出一个 JSON 对象，键为上述字段名，另加 "confidence"（0 到 1，'
        "你对整份抽取的把握）。只依据原文，不引入外部知识。"
    ),
    stage="extract_method",
)

# 缺口条目的种类枚举（GAPMAP 的证据类型口径，#665）：
#   gap            还没人解决的开放问题
#   contradiction  与其他工作相互矛盾的结论
#   uncertainty    作者明说「尚不确定 / 证据不足」的判断
#   negative_result 试过但失败/无效的尝试（负结果）
#   limitation     作者自述的方法/评测局限
GAP_KINDS = ("gap", "contradiction", "uncertainty", "negative_result", "limitation")

# 缺口与负结果台账（#665，设计报告 §11 燃料 2+4）。
#
# 与 skeleton@1 的 limitations 字段的分工：skeleton 的 limitations 是论文自述局限的
# **粗摘**（几句话概括，无出处，服务于「一眼看懂这篇论文」）；gaps@1 是**逐条挂原文
# 出处的细粒度台账**——每条必须带 source_span（≤200 字原文摘录）锚定出处，种类也
# 更细（缺口/矛盾/不确定/负结果/局限五类），服务于库级聚合与后续的假设生成燃料。
# 两者并存不算重复：粗摘可以无中生有地概括，台账的硬要求是「说得出这句话在原文
# 哪里」——无出处的条目宁可不要（归一化直接丢弃，见 runtime._normalize_entries）。
GAPS_SCHEMA = ExtractionSchema(
    id="gaps",
    version=1,
    stage="extract_gaps",
    fields=(
        SchemaField(
            "entries",
            "entries",
            max_len=0,  # entries 型不用字段级字符帽，各键的帽在 entry_keys 上
            max_items=8,
            entry_keys=(
                EntryKey("kind", max_len=32, choices=GAP_KINDS),
                EntryKey("statement", max_len=300),
                EntryKey("source_span", max_len=200),
            ),
        ),
    ),
    prompt_template=(
        "POLARIS_EXTRACT_GAPS\n"
        "你是论文缺口与负结果记录员。通读给定论文的标题与正文，找出其中明确提到的："
        "尚未解决的开放问题（gap）、与其他工作矛盾的结论（contradiction）、"
        "作者明说不确定的判断（uncertainty）、失败或无效的尝试（negative_result）、"
        "作者自述的局限（limitation）：\n"
        "{fields_spec}\n"
        "每条的 statement 用中文归纳（专有名词保留原文）；source_span 必须是"
        "**逐字摘自正文的原文片段**（保持原语言，不翻译不改写），用于锚定出处——"
        "找不到可摘录的原文依据的条目不要写。宁缺毋滥，只记论文明确说了的。\n"
        '只输出一个 JSON 对象，键为上述字段名，另加 "confidence"（0 到 1，'
        "你对整份抽取的把握）。只依据原文，不引入外部知识。"
    ),
)

_REGISTRY: dict[str, ExtractionSchema] = {}


def register_schema(schema: ExtractionSchema) -> None:
    """挂载一个抽取 schema（学科包的接入点）。同 id 重复注册视为版本演进，直接覆盖。"""
    _REGISTRY[schema.id] = schema


def get_schema(schema_id: str) -> ExtractionSchema:
    schema = _REGISTRY.get(schema_id)
    if schema is None:
        raise ValueError(f"unknown extraction schema: {schema_id!r}")
    return schema


def list_schemas() -> list[ExtractionSchema]:
    return list(_REGISTRY.values())


register_schema(SKELETON_SCHEMA)
register_schema(METHOD_SCHEMA)
register_schema(GAPS_SCHEMA)
