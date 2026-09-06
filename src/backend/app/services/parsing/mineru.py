"""MinerU（自托管 web api）适配器：POST PDF → markdown → 分节 / 表格 / 公式。

与 services/mineru.py（MinerU Cloud：签名 URL 上传 + 批次轮询，供版本化 PDF
生命周期用）是两套部署形态——这里对接的是 MinerU 仓库 projects/web_api 那类
自托管单次解析端点：``POST {base}/file_parse``（multipart 上传 PDF），响应 JSON
里带 markdown 正文。

响应形状按 MinerU web_api 文档/源码约定为 {"md_content": "..."}；不同版本键名
有出入（markdown / data.md_content 也见过），_markdown_of 按候选键顺序取第一个
命中的。**形状映射由 tests/fixtures/mineru_web_sample.json 锁定**（不打真网），
上游改形状时改 fixture + 候选键即可。
"""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path
from typing import Any

import httpx

from app.core.config import get_settings
from app.services.parsing.contract import (
    ParsedSection,
    ParseResult,
    compute_coverage,
)

logger = logging.getLogger(__name__)

MINERU_TIMEOUT_SECONDS = 300.0

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_TABLE_LINE_RE = re.compile(r"^\s*\|.*\|\s*$")
_HTML_TABLE_RE = re.compile(r"<table\b.*?</table>", re.IGNORECASE | re.DOTALL)
_FORMULA_RE = re.compile(r"\$\$(.+?)\$\$", re.DOTALL)


def _markdown_of(payload: Any) -> str:
    """响应 JSON → markdown 文本；候选键顺序即优先级（fixture 锁定）。"""
    if isinstance(payload, str):
        return payload
    if not isinstance(payload, dict):
        return ""
    for key in ("md_content", "markdown", "md"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value
    nested = payload.get("data") or payload.get("result")
    if isinstance(nested, (dict, str)):
        return _markdown_of(nested)
    return ""


def _extract_tables(markdown: str) -> list[dict[str, Any]]:
    tables: list[dict[str, Any]] = [
        {"html": block.strip()} for block in _HTML_TABLE_RE.findall(markdown)
    ]
    block: list[str] = []
    for line in [*markdown.splitlines(), ""]:
        if _TABLE_LINE_RE.match(line):
            block.append(line.strip())
            continue
        if len(block) >= 2:  # 单行竖线多半是正文噪声，不算表
            tables.append({"markdown": "\n".join(block)})
        block = []
    return tables


def _extract_formulas(markdown: str) -> list[dict[str, Any]]:
    formulas: list[dict[str, Any]] = []
    for latex in _FORMULA_RE.findall(markdown):
        latex = latex.strip()
        if latex:
            formulas.append({"latex": latex})
    return formulas


def parse_mineru_markdown(markdown: str) -> ParseResult | None:
    """markdown → ParseResult（纯函数，fixture 锁映射）。

    分节按 ATX 标题切；首个标题前的内容归入无标题节。表格 / 公式**另列**而不从
    节文本里抠掉——正文纯文本要保持完整（分段索引 / 正则参考文献兜底都吃它），
    tables / formulas 只是结构化补充。
    """
    markdown = markdown.replace("\r\n", "\n").strip()
    if not markdown:
        return None
    sections: list[ParsedSection] = []
    heading = ""
    lines: list[str] = []

    def flush() -> None:
        text = "\n".join(lines).strip()
        if heading or text:
            sections.append(ParsedSection(heading=heading, text=text))

    for line in markdown.splitlines():
        m = _HEADING_RE.match(line)
        if m:
            flush()
            heading = m.group(2).strip()
            lines = []
        else:
            lines.append(line)
    flush()
    result = ParseResult(
        sections=sections,
        tables=_extract_tables(markdown),
        formulas=_extract_formulas(markdown),
    )
    if not result.body_text():
        return None
    result.quality.coverage = compute_coverage(
        result, metadata=False, references=False, body=True
    )
    return result


class MineruWebAdapter:
    """MinerU 自托管解析适配器（POLARIS_MINERU_URL 配置，空 = 不可用）。"""

    name = "mineru"
    provides_metadata = False
    provides_references = False
    provides_body = True

    def available(self) -> bool:
        return bool(get_settings().mineru_parse_url.strip())

    async def parse(self, pdf_path: Path) -> ParseResult | None:
        base = get_settings().mineru_parse_url.strip().rstrip("/")
        content = await asyncio.to_thread(pdf_path.read_bytes)
        async with httpx.AsyncClient(timeout=MINERU_TIMEOUT_SECONDS) as client:
            response = await client.post(
                f"{base}/file_parse",
                files={"file": (pdf_path.name, content, "application/pdf")},
            )
            response.raise_for_status()
            try:
                payload = response.json()
            except ValueError:
                payload = response.text  # 个别部署直接回 markdown 文本
        markdown = _markdown_of(payload)
        if not markdown:
            logger.warning("MinerU web api returned no markdown content")
            return None
        return parse_mineru_markdown(markdown)
