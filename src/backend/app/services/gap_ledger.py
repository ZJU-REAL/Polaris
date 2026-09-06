"""库级缺口与负结果台账（#665，设计报告 §11 燃料 2+4，GAPMAP 式）。

数据从 paper_extractions 的 gaps@1 产物聚合（schemas.GAPS_SCHEMA，每条带原文
摘录锚定出处），本文件全是确定性代码：查表、拍平、过滤、排序、启发式配对——
判断性工作（逐篇抽条目）已在抽取环节由 LLM 做完，聚合层不再调模型。

矛盾对（contradiction pairs）本 PR 只做**同概念异立场的启发式配对**：两条 statement
共享足够多的概念词、且恰好一条含否定/反驳措辞，就配成一对并标 heuristic=True。
不做 LLM 矛盾判定的原因：ContraCrow 式的逐对语义判定要对 O(n²) 候选各打一次模型，
库一大成本失控，而台账页面是浏览型入口、日常打开率高——贵的判定应该按需触发
（用户点开某一对再细判），归后续批次；启发式误报由 heuristic 标记 + 双方原文
摘录兜底，读者一眼能自行核对。
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.library_direction import LibraryPaper
from app.models.paper import Paper
from app.models.paper_extraction import PaperExtraction
from app.services.extraction.schemas import GAP_KINDS, GAPS_SCHEMA

# 排序权重：kind 的优先级折算成「等效年数」加到论文年份上——gap/contradiction
# 是假设生成最值钱的燃料（别人没解决的 + 相互打架的），优先于负结果/不确定，
# 自述局限垫底（skeleton 也有粗摘，边际信息最少）。权重取 0..3 的小数量级，
# 让「新近度」与「种类」互相能翻盘：2026 年的局限排得过 2022 年的 gap，
# 但同龄论文里 gap 永远在局限前面。
_KIND_WEIGHTS = {
    "gap": 3,
    "contradiction": 3,
    "negative_result": 2,
    "uncertainty": 1,
    "limitation": 0,
}
# 没有年份的论文（预印本元数据缺失等）沉底：给不出新近度就别和有年份的抢位置
_NO_YEAR_SCORE = -1_000_000


@dataclass(slots=True)
class GapEntry:
    """台账里的一条：出自哪篇论文、什么种类、归纳陈述 + 原文摘录锚点。"""

    paper_id: uuid.UUID
    paper_title: str
    kind: str
    statement: str
    source_span: str
    year: int | None


def _entry_sort_key(entry: GapEntry) -> tuple:
    score = (
        _NO_YEAR_SCORE if entry.year is None else entry.year
    ) + _KIND_WEIGHTS.get(entry.kind, 0)
    # 主排序分之后再按年份、statement 定序：同分条目的顺序不随查询顺序漂移
    return (-score, -(entry.year or 0), entry.statement)


async def library_gaps(
    session: AsyncSession,
    library_id: uuid.UUID,
    *,
    kind: str | None = None,
    top: int = 50,
) -> list[GapEntry]:
    """聚合一个库内全部论文的缺口台账条目，排序后取前 top 条。

    成员口径：库内非回收站论文（status != excluded——被淘汰论文的缺口不该
    继续给这个方向供燃料）。排序 = 论文年份 + kind 权重（见 _KIND_WEIGHTS）。
    """
    if kind is not None and kind not in GAP_KINDS:
        raise ValueError(f"unknown gap kind: {kind!r}")
    stmt = (
        select(PaperExtraction, Paper)
        .join(Paper, Paper.id == PaperExtraction.paper_id)
        .join(LibraryPaper, LibraryPaper.paper_id == Paper.id)
        .where(
            LibraryPaper.library_id == library_id,
            LibraryPaper.status != "excluded",
            PaperExtraction.schema_id == GAPS_SCHEMA.id,
        )
    )
    entries: list[GapEntry] = []
    for extraction, paper in (await session.execute(stmt)).all():
        for item in extraction.payload.get("entries") or []:
            # 归一化在抽取时已做过；这里再做形状防御只为老产物/手工数据不炸接口
            if not isinstance(item, dict):
                continue
            entry_kind = item.get("kind")
            statement = item.get("statement")
            span = item.get("source_span")
            if not (entry_kind and statement and span):
                continue
            if kind is not None and entry_kind != kind:
                continue
            entries.append(
                GapEntry(
                    paper_id=paper.id,
                    paper_title=paper.title,
                    kind=entry_kind,
                    statement=statement,
                    source_span=span,
                    year=paper.year,
                )
            )
    entries.sort(key=_entry_sort_key)
    return entries[:top]


# ---- 矛盾对启发式（同概念异立场） ----

# 概念词提取：英文术语（≥4 字符的词，技术名词的主要形态）+ 中文三元组
# （无分词依赖的确定性近似；三元组比二元组少撞「方法/模型」这类高频泛词）。
_EN_TERM_RE = re.compile(r"[A-Za-z][A-Za-z0-9+_-]{3,}")
_CJK_RUN_RE = re.compile(r"[一-鿿]{3,}")
_EN_STOPWORDS = frozenset(
    {
        "this", "that", "with", "from", "have", "been", "does", "not",
        "only", "into", "over", "such", "than", "then", "them", "these",
        "those", "when", "which", "while", "will", "would", "could",
        "should", "there", "their", "about", "after", "before", "between",
        "under", "based", "using", "used", "more", "most", "less", "least",
        "fake",  # fake provider 的替身文案里到处都是，别拿它当共享概念
    }
)
# 否定/反驳措辞：英文按词边界（防 "notable" 撞 "not"），中文按多字子串
# （单字「不」误伤面太大，不收）。命中任一条即视为「反方立场」。
_EN_NEGATION_RE = re.compile(
    r"\b(?:not|no|nor|cannot|can't|fails?|failed|worse|negative|refutes?|"
    r"contradicts?|inconsistent|unable|without)\b",
    re.IGNORECASE,
)
_CJK_NEGATION_CUES = (
    "并未", "未能", "不能", "无法", "没有", "无人", "相反", "矛盾", "反驳",
    "失败", "失效", "劣于", "否定", "不成立", "不适用", "不显著", "无效",
)


def _concept_terms(text: str) -> frozenset[str]:
    terms = {
        w.lower() for w in _EN_TERM_RE.findall(text) if w.lower() not in _EN_STOPWORDS
    }
    for run in _CJK_RUN_RE.findall(text):
        terms.update(run[i : i + 3] for i in range(len(run) - 2))
    return frozenset(terms)


def _has_negation(text: str) -> bool:
    if _EN_NEGATION_RE.search(text):
        return True
    return any(cue in text for cue in _CJK_NEGATION_CUES)


# 至少共享几个概念词才算「谈的是同一件事」：1 个太容易被泛词/巧合命中，
# 2 个（一个英文术语 + 一个中文片段，或一个 4 字中文概念产出的两个三元组）
# 是「多半在谈同一概念」的最低门槛
_MIN_SHARED_TERMS = 2
_MAX_PAIRS = 20  # 配对是 O(n²) 里挑出来的展示位，页面上超过 20 对没人看


def find_contradiction_pairs(
    entries: list[GapEntry], *, limit: int = _MAX_PAIRS
) -> list[dict]:
    """在已排序的台账条目里找疑似矛盾对：同概念、异立场、不同论文。

    输入按 library_gaps 的排序给，配对按 (i, j) 字典序产出——排序靠前
    （新近且种类值钱）的条目优先占展示位。返回
    ``{"a", "b", "shared_terms", "heuristic": True}``；heuristic 恒 True，
    LLM 细判归后续批次（见模块 docstring 的成本论证）。
    """
    terms = [_concept_terms(e.statement) for e in entries]
    negated = [_has_negation(e.statement) for e in entries]
    pairs: list[dict] = []
    for i in range(len(entries)):
        for j in range(i + 1, len(entries)):
            if entries[i].paper_id == entries[j].paper_id:
                continue  # 同一篇论文自述的两条不算「相互矛盾的结论」
            if negated[i] == negated[j]:
                continue  # 立场同向（都肯定/都否定）不构成对立
            shared = terms[i] & terms[j]
            if len(shared) < _MIN_SHARED_TERMS:
                continue
            pairs.append(
                {
                    "a": entries[i],
                    "b": entries[j],
                    "shared_terms": sorted(shared)[:5],
                    "heuristic": True,
                }
            )
            if len(pairs) >= limit:
                return pairs
    return pairs
