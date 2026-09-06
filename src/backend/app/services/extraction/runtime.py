"""schema 引导抽取运行时（#661，设计报告 §10 ②层）。

一次抽取 = 取正文 → 按 schema 生成 prompt → LLM（extract_skeleton 环节，短 JSON
非流式）→ 校验归一化 → UPSERT 落 paper_extractions。判断性的只有「读文归纳」这一步
走 LLM；其余（正文选取、截断、归一化、落表）全是确定性代码。

正文口径：读 full_text_path 的 txt。双轨解析（#650）在裁决时已把 MinerU 的分节
正文拼回这份 txt（select.py 的 body 裁决），所以这里读 txt 就是「优先分节正文、
退回 PyMuPDF 全文」的口径，不必再碰解析工件。**没有全文就如实 skip**——摘要不
冒充正文：骨架抽取的价值在于全文证据（发现/局限多在正文后半段），拿摘要硬抽
只会产出置信度虚高的半成品。bibtex 导入这类无 PDF 论文因此零输出零副作用
（golden 链路不受影响）。

失败语义与 citation_graph 一致：无正文 / 模型输出解析不动 / 归一化后全空，
一律返回 skipped + 原因，不抛——批量回填时一篇论文的坏输出不该拖垮整批。
调用方负责 commit。
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.llm.base import Message
from app.models.paper import Paper
from app.models.paper_extraction import PaperExtraction
from app.services.extraction.schemas import ExtractionSchema, get_schema

logger = logging.getLogger(__name__)

# 正文进 prompt 的字符预算，与 wiki 编译同口径（wiki_compile.FULLTEXT_PROMPT_CHARS）。
# 不直接 import 那个常量：两处预算语义不同（编译要图文铺陈，抽取只要骨架），
# 将来允许各自调整，共享一个名字反而把它们焊死。
EXTRACT_PROMPT_CHARS = 24000

DEFAULT_SCHEMA_ID = "skeleton"


@dataclass(slots=True)
class ExtractionOutcome:
    """一次抽取的结果：extracted = 落了表；skipped = 没落表且 reason 说明为什么。"""

    status: str  # "extracted" | "skipped"
    reason: str | None = None
    row: PaperExtraction | None = None


def _read_body(paper: Paper) -> str | None:
    """论文正文（截断到预算）；无全文 / 文件缺失 / 内容为空一律 None。"""
    if not paper.full_text_path:
        return None
    path = Path(paper.full_text_path)
    if not path.exists():
        return None
    body = path.read_text(encoding="utf-8", errors="ignore").strip()
    return body[:EXTRACT_PROMPT_CHARS] or None


def _parse_json_object(content: str) -> dict[str, Any] | None:
    """模型输出 → 首个 JSON 对象；解析不动 / 不是对象返回 None。"""
    start, end = content.find("{"), content.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        payload = json.loads(content[start : end + 1])
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _normalize_text(value: Any, max_len: int) -> str | None:
    """text 字段归一化：只收字符串，去首尾空白，空串置 None，超长截断。"""
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text[:max_len] or None


def _normalize_list(value: Any, max_len: int, max_items: int | None) -> list[str] | None:
    """list 字段归一化：去空、去重（保序）、单条截断、条数封顶；空列表置 None。

    模型偶尔把单条内容直接给成字符串而不是单元素数组——收下它，这类形状偏差
    不值得整份产物作废。
    """
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return None
    items: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str):
            continue
        text = item.strip()[:max_len]
        if not text or text in seen:
            continue
        seen.add(text)
        items.append(text)
        if max_items is not None and len(items) >= max_items:
            break
    return items or None


def normalize_payload(
    schema: ExtractionSchema, raw: dict[str, Any]
) -> tuple[dict[str, Any], float | None]:
    """模型输出 → (归一化 payload, 置信度)。

    字段白名单：不在 schema 里的键直接丢弃（模型自作主张加的字段不入库）。
    空字段（空串 / 空列表 / 类型不对）不进 payload——「没抽到」用缺键表达，
    读方与前端据此不渲染，而不是渲染一个空段落。payload 可能为空 dict，
    调用方据此不落表。
    """
    payload: dict[str, Any] = {}
    for field in schema.fields:
        if field.kind == "list":
            value = _normalize_list(raw.get(field.name), field.max_len, field.max_items)
        else:
            value = _normalize_text(raw.get(field.name), field.max_len)
        if value is not None:
            payload[field.name] = value
    confidence: float | None = None
    raw_conf = raw.get("confidence")
    if isinstance(raw_conf, (int, float)) and not isinstance(raw_conf, bool):
        confidence = min(1.0, max(0.0, float(raw_conf)))
    return payload, confidence


def build_user_prompt(schema: ExtractionSchema, paper: Paper, body: str) -> str:
    """抽取 user prompt（标题行与 librarian 编译同格式，fake provider 靠它回显）。"""
    del schema  # 目前各 schema 共用同一份材料口径；字段差异全在 system prompt
    return (
        f"标题：{paper.title}\n"
        f"摘要：{(paper.abstract or '').strip() or '（无）'}\n"
        f"正文：\n{body}"
    )


async def extract_paper(
    session: AsyncSession,
    paper: Paper,
    *,
    schema_id: str = DEFAULT_SCHEMA_ID,
    llm: Any = None,
    user_id: uuid.UUID | None = None,
    project_id: uuid.UUID | None = None,
    library_id: uuid.UUID | None = None,
    force: bool = False,
) -> ExtractionOutcome:
    """对一篇论文跑一遍 schema 抽取；已有产物且非 force 时幂等跳过。调用方负责 commit。

    未知 schema_id 抛 ValueError——那是调用方代码写错，不是运行期波动，
    不能吞成 skip。其余失败一律 skipped + 原因。
    """
    schema = get_schema(schema_id)
    existing = await session.scalar(
        select(PaperExtraction).where(
            PaperExtraction.paper_id == paper.id,
            PaperExtraction.schema_id == schema.id,
        )
    )
    if existing is not None and not force:
        return ExtractionOutcome("skipped", "already extracted", existing)

    body = _read_body(paper)
    if body is None:
        return ExtractionOutcome("skipped", "no full text")

    if llm is None:
        from app.core.llm.router import get_llm_router

        llm = get_llm_router()
    result = await llm.complete(
        schema.stage,
        [
            Message(role="system", content=schema.system_prompt()),
            Message(role="user", content=build_user_prompt(schema, paper, body)),
        ],
        temperature=0.0,
        user_id=user_id,
        project_id=project_id,
        library_id=library_id,
    )
    raw = _parse_json_object(result.content)
    if raw is None:
        logger.warning("extraction output unparseable for paper %s (%s)", paper.id, schema.id)
        return ExtractionOutcome("skipped", "unparseable model output")
    payload, confidence = normalize_payload(schema, raw)
    if not payload:
        # 归一化后一个字段都没剩：与其存一行空产物骗过「已抽取」判定，不如不落表，
        # 下次重跑还有机会抽出来
        return ExtractionOutcome("skipped", "empty extraction")

    stage_meta = {"model": result.model, "stage": schema.stage, "version": schema.version}
    if existing is not None:
        existing.payload = payload
        existing.confidence = confidence
        existing.stage_meta = stage_meta
        row = existing
    else:
        row = PaperExtraction(
            paper_id=paper.id,
            schema_id=schema.id,
            payload=payload,
            confidence=confidence,
            stage_meta=stage_meta,
        )
        session.add(row)
    await session.flush()
    return ExtractionOutcome("extracted", None, row)


async def list_extractions(
    session: AsyncSession, paper_id: uuid.UUID
) -> list[PaperExtraction]:
    """一篇论文的全部抽取产物（详情页「结构化摘要」区的原料），按 schema_id 稳定排序。"""
    stmt = (
        select(PaperExtraction)
        .where(PaperExtraction.paper_id == paper_id)
        .order_by(PaperExtraction.schema_id)
    )
    return list((await session.execute(stmt)).scalars().all())
