"""引文边构建 + 引文意图分类（#639，设计报告 §10 ③层）。

解析产物就是 pdf_extract 落盘的纯文本全文（没有 GROBID 一类的结构化解析），
所以这里自带一套确定性的参考文献解析：

- 定位最后一个 References / Bibliography / 参考文献 标题行，之后是文献表；
- 编号式（[1] ...）按编号切条目；没有编号则按空行分段兜底；
- 引用上下文句：正文里含 [n] / [n,m] / [n-m] 标记的句子，按编号回挂到条目。

作者-年份式引用（Smith et al., 2020）解析不到上下文——意图分类降级用
「条目原文 + citing 论文标题摘要」，这正是任务里约定的降级路径。

意图分类走 citation_intent 环节（便宜小模型、短 JSON、批量 20 条一call）；
fake provider 下按上下文关键词给确定性结果（core/llm/fake.py 对齐）。
"""

import json
import logging
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.llm.base import Message
from app.models.paper import Paper
from app.models.paper_citation import CITATION_INTENTS, PaperCitation

logger = logging.getLogger(__name__)

# 解析护栏：超长论文/解析异常时不至于灌出海量边
MAX_REFERENCES = 300
MAX_REF_RAW_CHARS = 1000
MAX_CONTEXT_CHARS = 600
# 一次 LLM 调用最多分类多少条（短 JSON，20 条以内输出可控）
INTENT_BATCH_SIZE = 20

# 参考文献节标题（整行）；取**最后一个**匹配——正文里提到 "references" 的概率
# 远大于文献表后面还有一个同名标题
_REF_HEADING_RE = re.compile(
    r"^\s*(?:references|bibliography|参考文献)\s*:?\s*$", re.IGNORECASE | re.MULTILINE
)
# 编号式条目起点：行首 [12]
_NUM_ENTRY_RE = re.compile(r"^\s*\[(\d{1,3})\]", re.MULTILINE)
# 正文引用标记：[3] / [3,5] / [3-6]（数字与逗号/连字符的组合）
_CITE_MARK_RE = re.compile(r"\[(\d{1,3}(?:\s*[,\-–]\s*\d{1,3})*)\]")
# 句子边界（英文句号/问叹号 + 中文句读）
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?。！？])\s+")


@dataclass(slots=True)
class ParsedReference:
    index: int  # 1 起的参考文献序号
    raw: str  # 条目原文
    context: str | None = None  # 正文引用上下文句（解析不到为 None）


def split_reference_section(full_text: str) -> tuple[str, str]:
    """全文 → (正文, 参考文献节文本)；没有文献节时后者为空串。"""
    last = None
    for m in _REF_HEADING_RE.finditer(full_text):
        last = m
    if last is None:
        return full_text, ""
    return full_text[: last.start()], full_text[last.end() :]


def _entries_numbered(refs_text: str) -> list[tuple[int, str]]:
    marks = list(_NUM_ENTRY_RE.finditer(refs_text))
    entries: list[tuple[int, str]] = []
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(refs_text)
        raw = " ".join(refs_text[m.end() : end].split())
        if raw:
            entries.append((int(m.group(1)), raw[:MAX_REF_RAW_CHARS]))
    return entries


def _entries_fallback(refs_text: str) -> list[tuple[int, str]]:
    """无编号文献表：按空行分段；一整坨没有空行时按行给（arXiv 抽取常见形态）。"""
    blocks = [b for b in re.split(r"\n\s*\n", refs_text) if b.strip()]
    if len(blocks) <= 1:
        blocks = [line for line in refs_text.splitlines() if line.strip()]
    entries = []
    for i, block in enumerate(blocks, start=1):
        raw = " ".join(block.split())
        # 太短的行（页码、栏尾残句）不算条目
        if len(raw) >= 20:
            entries.append((i, raw[:MAX_REF_RAW_CHARS]))
    return entries


def _expand_marks(spec: str) -> list[int]:
    """引用标记内容 → 编号列表："3,5" → [3,5]；"3-6" → [3,4,5,6]。"""
    out: list[int] = []
    for part in re.split(r"\s*,\s*", spec):
        m = re.match(r"^(\d+)\s*[\-–]\s*(\d+)$", part)
        if m:
            lo, hi = int(m.group(1)), int(m.group(2))
            if lo <= hi and hi - lo <= 50:
                out.extend(range(lo, hi + 1))
        elif part.strip().isdigit():
            out.append(int(part))
    return out


def _citation_contexts(body: str) -> dict[int, str]:
    """正文 → {参考文献编号: 首个引用上下文句}。"""
    contexts: dict[int, str] = {}
    for sentence in _SENTENCE_SPLIT_RE.split(body):
        sentence = " ".join(sentence.split())
        if not sentence or len(sentence) < 15:
            continue
        for m in _CITE_MARK_RE.finditer(sentence):
            for n in _expand_marks(m.group(1)):
                contexts.setdefault(n, sentence[:MAX_CONTEXT_CHARS])
    return contexts


def parse_references(full_text: str) -> list[ParsedReference]:
    """全文 → 带上下文句的参考文献列表（确定性，无 LLM）。"""
    body, refs_text = split_reference_section(full_text)
    if not refs_text.strip():
        return []
    entries = _entries_numbered(refs_text)
    if len(entries) >= 2:
        contexts = _citation_contexts(body)
    else:
        entries = _entries_fallback(refs_text)
        contexts = {}  # 无编号 → 正文标记对不上号，全部走降级路径
    seen: set[int] = set()
    refs: list[ParsedReference] = []
    for index, raw in entries[:MAX_REFERENCES]:
        if index in seen:  # 解析噪声（正文残留的 [n]）不覆盖先到的条目
            continue
        seen.add(index)
        refs.append(ParsedReference(index=index, raw=raw, context=contexts.get(index)))
    return refs


def _norm_title(text: str) -> str:
    return " ".join(re.sub(r"[^0-9a-z一-鿿]+", " ", text.lower()).split())


async def _match_pool_papers(
    session: AsyncSession, refs: list[ParsedReference], *, citing_id: uuid.UUID
) -> dict[int, uuid.UUID]:
    """条目原文 ⊇ 池内论文标题（归一化后）→ 池内对齐。个人平台的池量级下全表标题可扫。"""
    rows = (await session.execute(select(Paper.id, Paper.title))).all()
    normalized_refs = {r.index: _norm_title(r.raw) for r in refs}
    matched: dict[int, uuid.UUID] = {}
    for paper_id, title in rows:
        if paper_id == citing_id:
            continue
        norm = _norm_title(title or "")
        if len(norm) < 20:  # 太短的标题子串匹配假阳性太高，不对齐
            continue
        for index, ref_norm in normalized_refs.items():
            if index not in matched and norm in ref_norm:
                matched[index] = paper_id
    return matched


async def ensure_citation_edges(
    session: AsyncSession, paper: Paper, *, force: bool = False
) -> int:
    """为一篇论文建引文边；已建过则跳过（force 重建先删后插）。调用方负责 commit。

    返回新建边数。没有全文（或全文里解析不出文献表）返回 0——增量钩子据此
    对 bibtex/每日推送这类无 PDF 论文零输出（golden 链路不受影响）。
    """
    existing = await session.scalar(
        select(func.count())
        .select_from(PaperCitation)
        .where(PaperCitation.citing_paper_id == paper.id)
    )
    if existing and not force:
        return 0
    if not paper.full_text_path or not Path(paper.full_text_path).exists():
        return 0
    full_text = Path(paper.full_text_path).read_text(encoding="utf-8", errors="ignore")
    refs = parse_references(full_text)
    if not refs:
        return 0
    if existing:
        for row in (
            await session.execute(
                select(PaperCitation).where(PaperCitation.citing_paper_id == paper.id)
            )
        ).scalars():
            await session.delete(row)
        await session.flush()
    matched = await _match_pool_papers(session, refs, citing_id=paper.id)
    for ref in refs:
        session.add(
            PaperCitation(
                citing_paper_id=paper.id,
                cited_paper_id=matched.get(ref.index),
                ref_index=ref.index,
                cited_ref_raw=ref.raw,
                context=ref.context,
            )
        )
    return len(refs)


# ---- 意图分类（citation_intent 环节） ----

_INTENT_SYSTEM_PROMPT = (
    "POLARIS_CITATION_INTENT\n"
    "你是引文意图分类器。输入 JSON 的 items 每条是论文里一处引用的上下文\n"
    "（context 是正文引用句；解析不到时是参考文献条目 + citing 论文的标题摘要）。\n"
    "对每条判定引用意图，五选一：\n"
    "background（背景铺垫）| method（方法沿用/扩展）| comparison（作为对比对象）|\n"
    "support（支持本文结论）| contrast（与本文观点相左）。\n"
    '只输出 JSON：{"items": [{"index": <原样回传>, "intent": "...", "confidence": 0到1}]}'
)


def _fallback_context(ref: PaperCitation, paper: Paper) -> str:
    """无上下文句的降级输入：条目原文 + citing 论文标题摘要。"""
    abstract = (paper.abstract or "")[:300]
    return f"[REF] {ref.cited_ref_raw}\n[CITING] {paper.title or ''}. {abstract}"[
        :MAX_CONTEXT_CHARS * 2
    ]


def _parse_intent_response(content: str) -> dict[int, tuple[str, float | None]]:
    start, end = content.find("{"), content.rfind("}")
    if start == -1 or end <= start:
        return {}
    try:
        payload = json.loads(content[start : end + 1])
    except json.JSONDecodeError:
        return {}
    out: dict[int, tuple[str, float | None]] = {}
    for item in payload.get("items") or []:
        if not isinstance(item, dict):
            continue
        intent = str(item.get("intent") or "")
        if intent not in CITATION_INTENTS or not isinstance(item.get("index"), int):
            continue
        confidence: float | None = None
        raw_conf = item.get("confidence")
        if isinstance(raw_conf, (int, float)):
            confidence = min(1.0, max(0.0, float(raw_conf)))
        out[int(item["index"])] = (intent, confidence)
    return out


async def classify_citation_intents(
    session: AsyncSession,
    paper: Paper,
    llm: Any,
    *,
    user_id: uuid.UUID | None = None,
    project_id: uuid.UUID | None = None,
    library_id: uuid.UUID | None = None,
) -> int:
    """把这篇论文 intent 还空着的引文边批量分类。调用方负责 commit，返回落库条数。

    批量便宜路径：20 条一次 complete（citation_intent 环节，短 JSON 档）。
    单批解析失败只丢那一批（记日志），不打断其余批次——批量任务跑全库时
    一篇论文的坏输出不该拖垮整个回填。
    """
    pending = (
        (
            await session.execute(
                select(PaperCitation)
                .where(
                    PaperCitation.citing_paper_id == paper.id,
                    PaperCitation.intent.is_(None),
                )
                .order_by(PaperCitation.ref_index)
            )
        )
        .scalars()
        .all()
    )
    if not pending:
        return 0
    classified = 0
    for i in range(0, len(pending), INTENT_BATCH_SIZE):
        batch = pending[i : i + INTENT_BATCH_SIZE]
        items = [
            {
                "index": ref.ref_index,
                "context": ref.context or _fallback_context(ref, paper),
            }
            for ref in batch
        ]
        result = await llm.complete(
            "citation_intent",
            [
                Message(role="system", content=_INTENT_SYSTEM_PROMPT),
                Message(role="user", content=json.dumps({"items": items}, ensure_ascii=False)),
            ],
            temperature=0.0,
            user_id=user_id,
            project_id=project_id,
            library_id=library_id,
        )
        parsed = _parse_intent_response(result.content)
        if not parsed:
            logger.warning(
                "citation intent batch unparseable for paper %s (batch %d)", paper.id, i
            )
            continue
        for ref in batch:
            hit = parsed.get(ref.ref_index)
            if hit is None:
                continue
            ref.intent, ref.confidence = hit
            classified += 1
    return classified


async def list_citations(
    session: AsyncSession, paper_id: uuid.UUID
) -> list[tuple[PaperCitation, str | None]]:
    """一篇论文的全部引文边 + 池内被引论文标题（详情页按 intent 分组的原料）。"""
    cited = select(Paper.id.label("pid"), Paper.title.label("ptitle")).subquery()
    stmt = (
        select(PaperCitation, cited.c.ptitle)
        .outerjoin(cited, cited.c.pid == PaperCitation.cited_paper_id)
        .where(PaperCitation.citing_paper_id == paper_id)
        .order_by(PaperCitation.ref_index)
    )
    return [(row[0], row[1]) for row in (await session.execute(stmt)).all()]
