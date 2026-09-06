"""GROBID 适配器：POST PDF → TEI XML → 元数据 + 结构化参考文献（+ 引用上下文句）。

自托管 GROBID 的标准接口是 ``POST {base}/api/processFulltextDocument``（multipart
上传 PDF，返回 TEI XML）。TEI 用标准库 xml.etree 解析——形状很规整，不值得为它
引入 lxml/BeautifulSoup 这类重依赖。

TEI 里对本模块重要的形状（GROBID 0.7/0.8 实测稳定）：
- 头部元数据：teiHeader/fileDesc（标题、作者、DOI、刊名、年份）+ profileDesc/abstract；
- 参考文献：text/back//div[@type="references"]/listBibl/biblStruct，每条带 xml:id
  （b0、b1…），开 includeRawCitations 时附 note[@type="raw_reference"] 原文；
- 引用上下文：正文段落里的 <ref type="bibr" target="#b3">[4]</ref> 标记——把段落
  按句切开，含标记的那句就是该条目的上下文句（确定性，无 LLM）。
"""

from __future__ import annotations

import asyncio
import logging
import re
import xml.etree.ElementTree as ET
from pathlib import Path

import httpx

from app.core.config import get_settings
from app.services.parsing.contract import (
    ParsedRef,
    ParsedSection,
    ParseResult,
    compute_coverage,
)

logger = logging.getLogger(__name__)

GROBID_TIMEOUT_SECONDS = 120.0
# 每条参考文献最多留几句上下文（防个别高被引条目把报告撑爆）
MAX_CONTEXTS_PER_REF = 5
MAX_CONTEXT_CHARS = 600

_TEI_NS = "http://www.tei-c.org/ns/1.0"
_XML_ID = "{http://www.w3.org/XML/1998/namespace}id"
# 句子边界（与 citation_graph 同口径：英文句读 + 中文句读）
_SENT_BOUNDARY_RE = re.compile(r"(?<=[.!?。！？])\s+")


def _t(tag: str) -> str:
    return f"{{{_TEI_NS}}}{tag}"


def _text(element: ET.Element | None) -> str:
    if element is None:
        return ""
    return " ".join("".join(element.itertext()).split())


def _person_name(pers: ET.Element) -> str:
    parts = [_text(el) for el in pers.findall(_t("forename"))]
    parts.append(_text(pers.find(_t("surname"))))
    return " ".join(p for p in parts if p)


def _authors_of(bibl: ET.Element) -> list[str]:
    names: list[str] = []
    for author in bibl.iter(_t("author")):
        pers = author.find(_t("persName"))
        if pers is None:
            continue
        name = _person_name(pers)
        if name:
            names.append(name)
    return names


def _year_of(bibl: ET.Element) -> int | None:
    for date in bibl.iter(_t("date")):
        when = date.get("when") or ""
        m = re.match(r"^(\d{4})", when or _text(date))
        if m:
            return int(m.group(1))
    return None


def _doi_of(bibl: ET.Element) -> str | None:
    for idno in bibl.iter(_t("idno")):
        if (idno.get("type") or "").upper() == "DOI":
            doi = _text(idno)
            if doi:
                return doi
    return None


def _header_metadata(root: ET.Element) -> dict:
    header = root.find(_t("teiHeader"))
    if header is None:
        return {}
    metadata: dict = {}
    title = _text(header.find(f"{_t('fileDesc')}/{_t('titleStmt')}/{_t('title')}"))
    if title:
        metadata["title"] = title
    bibl = header.find(f"{_t('fileDesc')}/{_t('sourceDesc')}/{_t('biblStruct')}")
    if bibl is not None:
        authors = _authors_of(bibl)
        if authors:
            metadata["authors"] = authors
        year = _year_of(bibl)
        if year is not None:
            metadata["year"] = year
        doi = _doi_of(bibl)
        if doi:
            metadata["doi"] = doi
        # 刊名 / 会议名：monogr/title（期刊 level="j"、专著 level="m"，都可作 venue）
        venue = _text(bibl.find(f"{_t('monogr')}/{_t('title')}"))
        if venue and venue != metadata.get("title"):
            metadata["venue"] = venue
    abstract = _text(header.find(f"{_t('profileDesc')}/{_t('abstract')}"))
    if abstract:
        metadata["abstract"] = abstract
    return metadata


def _paragraph_text_with_refs(p: ET.Element) -> tuple[str, list[tuple[str, int]]]:
    """段落 → (纯文本, [(参考文献 xml:id, 在文本中的偏移)])。

    偏移记录 <ref type="bibr"> 标记出现的位置，供按句切分后把上下文句挂回条目。
    """
    parts: list[str] = []
    refs: list[tuple[str, int]] = []
    pos = 0

    def add(text: str) -> None:
        nonlocal pos
        parts.append(text)
        pos += len(text)

    def walk(el: ET.Element) -> None:
        if el.tag == _t("ref") and (el.get("type") or "") == "bibr":
            target = (el.get("target") or "").lstrip("#")
            if target:
                refs.append((target, pos))
        if el.text:
            add(el.text)
        for child in el:
            walk(child)
            if child.tail:
                add(child.tail)

    walk(p)
    return "".join(parts), refs


def _sentence_spans(text: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    start = 0
    for m in _SENT_BOUNDARY_RE.finditer(text):
        spans.append((start, m.start()))
        start = m.end()
    spans.append((start, len(text)))
    return spans


def _sections_and_contexts(
    root: ET.Element,
) -> tuple[list[ParsedSection], dict[str, list[str]]]:
    """正文 div → 分节；同时按 <ref type="bibr"> 标记收集每条参考文献的上下文句。"""
    sections: list[ParsedSection] = []
    contexts: dict[str, list[str]] = {}
    body = root.find(f"{_t('text')}/{_t('body')}")
    if body is None:
        return sections, contexts
    for div in body.findall(_t("div")):
        heading = _text(div.find(_t("head")))
        paragraphs: list[str] = []
        for p in div.findall(_t("p")):
            text, refs = _paragraph_text_with_refs(p)
            text_norm = " ".join(text.split())
            if text_norm:
                paragraphs.append(text_norm)
            if not refs:
                continue
            spans = _sentence_spans(text)
            for target, offset in refs:
                sentence = ""
                for s, e in spans:
                    if s <= offset < e or (offset >= e and (s, e) == spans[-1]):
                        sentence = " ".join(text[s:e].split())
                        break
                if not sentence:
                    continue
                bucket = contexts.setdefault(target, [])
                sentence = sentence[:MAX_CONTEXT_CHARS]
                if len(bucket) < MAX_CONTEXTS_PER_REF and sentence not in bucket:
                    bucket.append(sentence)
        if heading or paragraphs:
            sections.append(ParsedSection(heading=heading, text="\n\n".join(paragraphs)))
    return sections, contexts


def _compose_raw(title: str | None, authors: list[str], year: int | None) -> str:
    pieces = []
    if authors:
        pieces.append(", ".join(authors))
    if year is not None:
        pieces.append(f"({year})")
    if title:
        pieces.append(title)
    return ". ".join(pieces)


def _references(root: ET.Element, contexts: dict[str, list[str]]) -> list[ParsedRef]:
    refs: list[ParsedRef] = []
    back = root.find(f"{_t('text')}/{_t('back')}")
    if back is None:
        return refs
    for div in back.iter(_t("div")):
        if (div.get("type") or "") != "references":
            continue
        for bibl in div.iter(_t("biblStruct")):
            rid = bibl.get(_XML_ID) or ""
            title = None
            analytic_title = bibl.find(f"{_t('analytic')}/{_t('title')}")
            monogr_title = bibl.find(f"{_t('monogr')}/{_t('title')}")
            title = _text(analytic_title) or _text(monogr_title) or None
            authors = _authors_of(bibl)
            year = _year_of(bibl)
            raw = ""
            for note in bibl.findall(_t("note")):
                if (note.get("type") or "") == "raw_reference":
                    raw = _text(note)
                    break
            raw = raw or _compose_raw(title, authors, year)
            if not raw:
                continue  # 完全空的条目（解析噪声）不进产物
            refs.append(
                ParsedRef(
                    raw=raw,
                    title=title,
                    authors=authors,
                    year=year,
                    doi=_doi_of(bibl),
                    contexts=list(contexts.get(rid, [])),
                )
            )
    return refs


def parse_tei(tei_xml: str) -> ParseResult | None:
    """TEI XML → ParseResult（纯函数，测试用 fixture 直接锁这里的映射）。"""
    try:
        root = ET.fromstring(tei_xml)
    except ET.ParseError:
        logger.warning("GROBID returned unparseable TEI", exc_info=True)
        return None
    sections, contexts = _sections_and_contexts(root)
    result = ParseResult(
        metadata=_header_metadata(root),
        references=_references(root, contexts),
        sections=sections,
    )
    if not result.metadata and not result.references and not result.sections:
        return None  # 空响应视同解析失败，让选优走 fallback
    result.quality.coverage = compute_coverage(
        result, metadata=True, references=True, body=False
    )
    return result


class GrobidAdapter:
    """GROBID 解析适配器（POLARIS_GROBID_URL 配置，空 = 不可用）。

    provides_body=False：TEI 正文分节只用来定位引用上下文句；正文来源的选优只在
    「MinerU / 现有 PyMuPDF 全文」之间裁决（见 select.py 的裁决矩阵），GROBID 的
    正文抽取本就不是它的强项，不参与竞争。
    """

    name = "grobid"
    provides_metadata = True
    provides_references = True
    provides_body = False

    def available(self) -> bool:
        return bool(get_settings().grobid_url.strip())

    async def parse(self, pdf_path: Path) -> ParseResult | None:
        base = get_settings().grobid_url.strip().rstrip("/")
        content = await asyncio.to_thread(pdf_path.read_bytes)
        async with httpx.AsyncClient(timeout=GROBID_TIMEOUT_SECONDS) as client:
            response = await client.post(
                f"{base}/api/processFulltextDocument",
                files={"input": (pdf_path.name, content, "application/pdf")},
                # 要 raw_reference 原文：citation_graph 的条目原文口径与正则路径一致
                data={"includeRawCitations": "1"},
            )
            response.raise_for_status()
        return parse_tei(response.text)
