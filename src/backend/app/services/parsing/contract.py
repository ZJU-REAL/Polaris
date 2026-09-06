"""解析双轨适配器的公共契约（#650，设计报告 §10 ①层）。

为什么要一层契约：现有解析只有 PyMuPDF 纯文本（literature/pdf_extract.py），
参考文献靠正则（citation_graph.py）、图表靠启发式裁剪。GROBID / MinerU 这类
结构化解析服务能给出质量高得多的元数据 / 参考文献 / 正文分节，但它们都是
可选的外部部署——没配就必须完全退回现状。契约把「解析产物长什么样」与
「谁解析的」解耦，选优逻辑（select.py）只面向 ParseResult 做逐字段裁决。

适配器为什么不挂 literature/runtime.py 的 AdapterRegistry：那张注册表是检索源
（search(request) -> SourceSearchPage）的依赖注入容器，按管理员运行时凭据构建、
带指纹缓存；解析适配器是「PDF 进、结构化产物出」的另一类协议，只有两个成员、
可用性只看环境变量，硬塞同一注册表只会让两边的类型都变含糊——故独立成小协议。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

# 元数据字段全集（选优逐字段裁决 / coverage 计分共用）
METADATA_FIELDS = ("title", "authors", "year", "venue", "doi", "abstract")

# coverage 里「正文够长」的字符数基准：短于此按比例折算（多数论文正文远超此值）
_BODY_FULL_CHARS = 5000


@dataclass(slots=True)
class ParsedRef:
    """一条结构化参考文献；contexts 是正文里引用它的上下文句（可空）。"""

    raw: str
    title: str | None = None
    authors: list[str] = field(default_factory=list)
    year: int | None = None
    doi: str | None = None
    contexts: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ParsedSection:
    heading: str
    text: str


@dataclass(slots=True)
class ParseQuality:
    coverage: float = 0.0  # 0..1，算法见 compute_coverage
    errors: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ParseResult:
    """一次解析的全部产物。适配器只填自己声明能力对应的字段，其余留空。"""

    metadata: dict[str, Any] = field(default_factory=dict)  # 键取 METADATA_FIELDS 子集
    references: list[ParsedRef] = field(default_factory=list)
    sections: list[ParsedSection] = field(default_factory=list)
    tables: list[dict[str, Any]] = field(default_factory=list)  # 可选：{"markdown"|"html": ...}
    formulas: list[dict[str, Any]] = field(default_factory=list)  # 可选：{"latex": ...}
    quality: ParseQuality = field(default_factory=ParseQuality)

    def body_text(self) -> str:
        """分节拼回正文纯文本（保留标题行，供分段索引 / 检索 / 正则兜底解析用）。"""
        parts: list[str] = []
        for section in self.sections:
            heading = section.heading.strip()
            text = section.text.strip()
            if heading and text:
                parts.append(f"{heading}\n\n{text}")
            elif heading or text:
                parts.append(heading or text)
        return "\n\n".join(parts)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@runtime_checkable
class ParsingAdapter(Protocol):
    """解析适配器协议。

    能力旗标声明产物参与哪些字段的选优（select.py 只在旗标为真时才考虑对应产物）；
    ``available()`` 只看配置、不出网——golden 链路不配 URL 时必须零副作用地判 False；
    ``parse()`` 出网失败允许抛异常（编排层捕获并记入质检报告），响应拿到但内容
    不可用时返回 None。
    """

    name: str
    provides_metadata: bool
    provides_references: bool
    provides_body: bool

    def available(self) -> bool: ...

    async def parse(self, pdf_path: Path) -> ParseResult | None: ...


def compute_coverage(
    result: ParseResult,
    *,
    metadata: bool,
    references: bool,
    body: bool,
) -> float:
    """按适配器声明的能力算覆盖度（各能力等权平均，0..1）。

    - metadata：六个字段里非空的占比；
    - references：有条目为 1，否则 0；
    - body：正文字符数对 _BODY_FULL_CHARS 的比例（封顶 1）。
    确定性纯函数：质检报告里的 coverage 必须可复算、可单测。
    """
    parts: list[float] = []
    if metadata:
        present = sum(1 for key in METADATA_FIELDS if _non_empty(result.metadata.get(key)))
        parts.append(present / len(METADATA_FIELDS))
    if references:
        parts.append(1.0 if result.references else 0.0)
    if body:
        parts.append(min(1.0, len(result.body_text()) / _BODY_FULL_CHARS))
    if not parts:
        return 0.0
    return round(sum(parts) / len(parts), 4)


def _non_empty(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, dict)):
        return len(value) > 0
    return True
