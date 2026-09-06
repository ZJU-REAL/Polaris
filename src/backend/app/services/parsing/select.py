"""双轨解析的质检选优 + 编排 + 产物落盘（#650）。

裁决矩阵（逐字段，确定性）：
- metadata：GROBID 有值的字段用 GROBID，缺的字段回退现有值（arXiv/DOI 解析或
  PyMuPDF 路径已有的元数据）；
- references：GROBID 可用且条目数 > 0 → 用结构化条目（落 references.json，
  citation_graph 优先吃它）；否则不落文件，引文建边保持原正则路径；
- body/sections：MinerU 可用且正文非空 → 用 MinerU 分节正文；否则用现有
  PyMuPDF 全文。

产物全部是文件工件，挂在既有的 <data_dir>/papers/<paper_id>/ 目录下（figures/
已是先例，论文删除时整目录 rmtree，见 services/papers.py）——不新增表、不加列：
- parse_quality.json：per-document 质检报告（两轨可用性 / coverage / 错误 / 裁决）；
- references.json：结构化参考文献（含上下文句）。

golden 保全：两个适配器都没配 URL 时，编排入口 extract_with_dual_track 直接调用
原 extract_full_text 并且不写任何附加文件——与现状逐字节一致。
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.services.parsing.contract import (
    METADATA_FIELDS,
    ParseResult,
    ParsingAdapter,
    _non_empty,
)
from app.services.parsing.grobid import GrobidAdapter
from app.services.parsing.mineru import MineruWebAdapter

logger = logging.getLogger(__name__)

QUALITY_REPORT_VERSION = 1
REFERENCES_FILE_VERSION = 1


def parsing_adapters() -> tuple[ParsingAdapter, ParsingAdapter]:
    return (GrobidAdapter(), MineruWebAdapter())


def dual_track_available() -> bool:
    """任一解析适配器配置了 URL？（只看配置，不出网。）"""
    return any(adapter.available() for adapter in parsing_adapters())


def parse_quality_path(paper_id: str) -> Path:
    from app.services.literature.pdf_extract import papers_dir

    return papers_dir() / str(paper_id) / "parse_quality.json"


def structured_references_path(paper_id: str) -> Path:
    from app.services.literature.pdf_extract import papers_dir

    return papers_dir() / str(paper_id) / "references.json"


def load_structured_references(paper_id: str) -> list[dict[str, Any]]:
    """读结构化参考文献工件；没有 / 读不动一律空列表（调用方回退正则路径）。"""
    path = structured_references_path(paper_id)
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.warning("unreadable references.json for paper %s", paper_id, exc_info=True)
        return []
    refs = payload.get("references") if isinstance(payload, dict) else None
    return [item for item in refs or [] if isinstance(item, dict) and item.get("raw")]


@dataclass(slots=True)
class SelectionOutcome:
    """逐字段裁决结果 + 质检报告（编排层据此落盘）。"""

    body: str
    metadata: dict[str, Any]
    references: list[dict[str, Any]] | None  # None = 保持正则 fallback，不落文件
    quality_report: dict[str, Any]


def _adapter_report(
    adapter: ParsingAdapter, result: ParseResult | None, errors: list[str]
) -> dict[str, Any]:
    return {
        "available": adapter.available(),
        "parsed": result is not None,
        "coverage": result.quality.coverage if result is not None else None,
        "errors": errors,
    }


def select_parse(
    *,
    grobid: ParseResult | None,
    mineru: ParseResult | None,
    fallback_text: str,
    fallback_metadata: dict[str, Any] | None = None,
    grobid_errors: list[str] | None = None,
    mineru_errors: list[str] | None = None,
    grobid_adapter: ParsingAdapter | None = None,
    mineru_adapter: ParsingAdapter | None = None,
) -> SelectionOutcome:
    """逐字段裁决（纯函数，裁决矩阵见模块 docstring；三种可用性组合都在单测里锁死）。"""
    grobid_adapter = grobid_adapter or GrobidAdapter()
    mineru_adapter = mineru_adapter or MineruWebAdapter()

    # metadata：GROBID 有值的字段优先，缺字段回退现有值
    metadata = {key: (fallback_metadata or {}).get(key) for key in METADATA_FIELDS}
    metadata_decision = "fallback"
    if grobid is not None and grobid_adapter.provides_metadata:
        used = False
        for key in METADATA_FIELDS:
            value = grobid.metadata.get(key)
            if _non_empty(value):
                metadata[key] = value
                used = True
        if used:
            metadata_decision = "grobid"

    # references：GROBID 条目数 > 0 才换轨，否则保持正则 fallback
    references: list[dict[str, Any]] | None = None
    references_decision = "fallback"
    if grobid is not None and grobid_adapter.provides_references and grobid.references:
        references = [
            {
                "raw": ref.raw,
                "title": ref.title,
                "authors": ref.authors,
                "year": ref.year,
                "doi": ref.doi,
                "contexts": ref.contexts,
            }
            for ref in grobid.references
        ]
        references_decision = "grobid"

    # body：MinerU 正文非空才换轨，否则用现有全文
    body = fallback_text
    body_decision = "fallback"
    if mineru is not None and mineru_adapter.provides_body:
        mineru_body = mineru.body_text()
        if mineru_body:
            body = mineru_body
            body_decision = "mineru"

    report = {
        "version": QUALITY_REPORT_VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "adapters": {
            grobid_adapter.name: _adapter_report(grobid_adapter, grobid, grobid_errors or []),
            mineru_adapter.name: _adapter_report(mineru_adapter, mineru, mineru_errors or []),
        },
        "decisions": {
            "metadata": metadata_decision,
            "references": references_decision,
            "body": body_decision,
        },
        "counts": {
            "references": len(references or []),
            "sections": len(mineru.sections) if mineru is not None else 0,
            "tables": len(mineru.tables) if mineru is not None else 0,
            "formulas": len(mineru.formulas) if mineru is not None else 0,
            "body_chars": len(body),
        },
    }
    return SelectionOutcome(
        body=body, metadata=metadata, references=references, quality_report=report
    )


@dataclass(slots=True)
class DualTrackOutcome:
    txt_path: Path
    metadata: dict[str, Any] = field(default_factory=dict)
    quality_report: dict[str, Any] | None = None
    used_adapters: bool = False


async def _run_adapter(
    adapter: ParsingAdapter, pdf_path: Path
) -> tuple[ParseResult | None, list[str]]:
    """单轨解析，失败不抛（批处理语义：坏一轨不拖垮另一轨），错误进质检报告。"""
    if not adapter.available():
        return None, []
    try:
        result = await adapter.parse(pdf_path)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 — 外部服务隔离，错误只进报告
        logger.warning("%s parse failed for %s", adapter.name, pdf_path, exc_info=True)
        return None, [f"{type(exc).__name__}: {exc}"[:300]]
    if result is None:
        return None, ["EMPTY_RESULT"]
    return result, []


async def extract_with_dual_track(
    paper_id: str,
    pdf_path: Path,
    *,
    fallback_metadata: dict[str, Any] | None = None,
) -> DualTrackOutcome:
    """enrich 链 extract 步骤的双轨入口：适配器可用 → 双轨解析 → 选优 → 落地。

    两轨都没配时**逐字节等价**于原 extract_full_text：写同一份 txt、不产生任何
    附加文件（golden 链路不配 URL，走的就是这条路）。
    """
    from app.services.literature.pdf_extract import extract_full_text, sanitize_text

    # fallback 全文永远先算：它既是 body 的兜底，也保证任何一轨失败都有可用产物
    txt_path = await extract_full_text(paper_id, pdf_path)
    grobid_adapter, mineru_adapter = parsing_adapters()
    if not grobid_adapter.available() and not mineru_adapter.available():
        return DualTrackOutcome(txt_path=txt_path)

    fallback_text = txt_path.read_text(encoding="utf-8", errors="ignore")
    grobid_result, grobid_errors = await _run_adapter(grobid_adapter, pdf_path)
    mineru_result, mineru_errors = await _run_adapter(mineru_adapter, pdf_path)
    outcome = select_parse(
        grobid=grobid_result,
        mineru=mineru_result,
        fallback_text=fallback_text,
        fallback_metadata=fallback_metadata,
        grobid_errors=grobid_errors,
        mineru_errors=mineru_errors,
        grobid_adapter=grobid_adapter,
        mineru_adapter=mineru_adapter,
    )

    if outcome.quality_report["decisions"]["body"] == "mineru":
        txt_path.write_text(sanitize_text(outcome.body), encoding="utf-8")

    refs_path = structured_references_path(paper_id)
    refs_path.parent.mkdir(parents=True, exist_ok=True)
    if outcome.references is not None:
        refs_path.write_text(
            json.dumps(
                {
                    "version": REFERENCES_FILE_VERSION,
                    "source": "grobid",
                    "references": outcome.references,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    else:
        # 这次拿不到结构化条目就清掉旧文件：引文建边宁可用当前全文重跑正则，
        # 也不能吃上一版 PDF 的陈旧结构化条目
        refs_path.unlink(missing_ok=True)

    quality_path = parse_quality_path(paper_id)
    quality_path.write_text(
        json.dumps(outcome.quality_report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return DualTrackOutcome(
        txt_path=txt_path,
        metadata=outcome.metadata,
        quality_report=outcome.quality_report,
        used_adapters=True,
    )


def paper_fallback_metadata(paper: Any) -> dict[str, Any]:
    """Paper 行当前元数据 → 选优的 fallback 值（authors 统一成名字列表）。"""
    names: list[str] = []
    for item in paper.authors or []:
        name = item.get("name") if isinstance(item, dict) else item
        if name and str(name).strip():
            names.append(str(name).strip())
    return {
        "title": paper.title,
        "authors": names,
        "year": paper.year,
        "venue": paper.venue,
        "doi": paper.doi,
        "abstract": paper.abstract,
    }


def apply_missing_metadata(paper: Any, metadata: dict[str, Any]) -> bool:
    """把裁决后的元数据写回 Paper 行——**只补空字段**。

    完整裁决产物在质检报告里；写回时不覆盖已有值，因为 resolve 阶段来自
    arXiv/DOI 的权威元数据不该被 PDF 解析结果顶掉。返回是否有改动。
    """
    changed = False
    if not (paper.abstract or "").strip() and _non_empty(metadata.get("abstract")):
        paper.abstract = str(metadata["abstract"])
        changed = True
    if paper.year is None and isinstance(metadata.get("year"), int):
        paper.year = metadata["year"]
        changed = True
    if not (paper.venue or "").strip() and _non_empty(metadata.get("venue")):
        paper.venue = str(metadata["venue"])[:255]
        changed = True
    if not (paper.doi or "").strip() and _non_empty(metadata.get("doi")):
        paper.doi = str(metadata["doi"])[:255]
        changed = True
    if not paper.authors and _non_empty(metadata.get("authors")):
        paper.authors = [{"name": str(name)} for name in metadata["authors"]]
        changed = True
    return changed
