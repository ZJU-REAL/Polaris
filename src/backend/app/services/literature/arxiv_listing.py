"""解析 arXiv 的每日公告列表页（``/list/{category}/new``）。

**为什么不用 RSS。** ``rss.arxiv.org`` 会按分类各自陈旧：2026-08-12 生产上
cs.AI 的 RSS 供的是上一批（最大 id ``2608.09930``），而同一时刻网页列表已经是
``2608.11204``，两边只重合 16 篇；cs.CL 的 RSS 却是新的。RSS 还把 ``lastBuildDate``
写成当天，所以这种陈旧**没有任何迹象**——条目几百条，去重后一条不进，每一步都报成功。

列表页在三件事上更适合当数据源：

1. 公告日期是页面自己写的（``Showing new listings for Wednesday, 12 August 2026``），
   不像 RSS 的 ``pubDate`` 会整体滞后一天；
2. New / Cross / Replacement 分段标注，和我们「只收 new+cross」的口径天然对齐；
3. **每段自带条数**（``showing 79 of 79 entries``），于是「解析坏了」和「今天没公告」
   可以区分开——这正是 RSS 给不了的，也是那次漏收一整天没被发现的原因。

代价是依赖 HTML 结构。所以这里的原则是**宁可炸掉也不要静默返回空**：条数对不上就抛
:class:`ArxivListingError`，交给调用方按失败处理。
"""

import logging
import re
from html import unescape
from typing import Any

from app.services.literature.arxiv import (
    ABS_URL_TEMPLATE,
    PDF_URL_TEMPLATE,
    normalize_arxiv_id,
)

logger = logging.getLogger(__name__)

LISTING_URL_TEMPLATE = "https://arxiv.org/list/{category}/new"

#: 页面自己声明的每页上限。显式带上，不吃「默认给全量」这个随时会变的行为。
LISTING_PAGE_SIZE = 2000

#: 只收这两段。Replacement 是旧论文更新，不是当天新公告。
_WANTED_SECTIONS = ("new", "cross")

_SECTION_RE = re.compile(
    r"<h3[^>]*>\s*(New|Cross|Replacement)\s+submissions?\s*"
    r"\(showing\s+(?:first\s+)?(\d+)\s+of\s+(\d+)\s+entries\)",
    re.I,
)
_HEADER_DATE_RE = re.compile(
    r"<h3[^>]*>\s*Showing new listings for\s+(?:\w+day),?\s*(\d{1,2}\s+\w+\s+\d{4})", re.I
)
_ENTRY_RE = re.compile(r"<dt>(?P<dt>.*?)</dt>\s*<dd>(?P<dd>.*?)</dd>", re.S)
_ARXIV_ID_RE = re.compile(r"arXiv:(\d{4}\.\d{4,5})")
_TAG_RE = re.compile(r"<[^>]+>")


class ArxivListingError(RuntimeError):
    """列表页取不到或解析不出来。**绝不退化成空结果。**"""


def _text(fragment: str) -> str:
    return unescape(_TAG_RE.sub(" ", fragment)).replace("\xa0", " ").strip()


def _collapse(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _section_spans(html: str) -> list[tuple[str, int, int, int, int]]:
    """切出各段：(名字, 本页条数, 总条数, 正文起点, 正文终点)。"""
    marks = list(_SECTION_RE.finditer(html))
    spans: list[tuple[str, int, int, int, int]] = []
    for index, mark in enumerate(marks):
        end = marks[index + 1].start() if index + 1 < len(marks) else len(html)
        spans.append(
            (mark.group(1).lower(), int(mark.group(2)), int(mark.group(3)), mark.end(), end)
        )
    return spans


def _div(dd_html: str, class_name: str) -> str | None:
    """取 ``<div class='list-xxx'>`` 的内容。

    按 class 取，不在拍平后的文本里按 ``Title:`` / ``Subjects:`` 这类字样切——
    页面里标题和作者之间没有任何标签词，纯文本切法会把作者名粘进标题，
    而这种错不会报任何异常，只会让库里多出一堆标题带人名的论文。
    """
    match = re.search(
        rf"<div[^>]*class=['\"][^'\"]*\b{class_name}\b[^'\"]*['\"][^>]*>(.*?)</div>",
        dd_html,
        re.S | re.I,
    )
    return match.group(1) if match else None


def _strip_descriptor(fragment: str) -> str:
    """去掉 ``<span class='descriptor'>Title:</span>`` 这类前缀标签。"""
    return re.sub(
        r"<span[^>]*class=['\"][^'\"]*descriptor[^'\"]*['\"][^>]*>.*?</span>",
        " ",
        fragment,
        flags=re.S | re.I,
    )


def _parse_entry(dt_html: str, dd_html: str) -> dict[str, Any] | None:
    """把一条 dt/dd 解析成与 RSS 条目同形状的 dict。

    id **只从 dt 里取**：dd 是摘要正文，里面完全可能提到别的 arXiv 编号，
    拿正则扫全页会凭空多收论文（实测某天页面上唯一 id 比 ``Total of`` 多一个）。
    """
    id_match = _ARXIV_ID_RE.search(dt_html)
    if id_match is None:
        return None
    arxiv_id = normalize_arxiv_id(id_match.group(1))

    title_html = _div(dd_html, "list-title")
    title = _collapse(_text(_strip_descriptor(title_html))) if title_html else ""
    if not arxiv_id or not title:
        return None

    authors_html = _div(dd_html, "list-authors") or ""
    names = [_collapse(_text(a)) for a in re.findall(r"<a[^>]*>(.*?)</a>", authors_html, re.S)]
    authors = [{"name": n} for n in names if n] or None

    subjects_html = _div(dd_html, "list-subjects") or ""
    primary_match = re.search(
        r"<span[^>]*class=['\"][^'\"]*primary-subject[^'\"]*['\"][^>]*>(.*?)</span>",
        subjects_html,
        re.S | re.I,
    )
    subjects_text = _text(_strip_descriptor(subjects_html))
    categories = re.findall(r"\(([a-zA-Z\-]+\.[a-zA-Z\-]+)\)", subjects_text)
    primary = None
    if primary_match:
        found = re.search(r"\(([a-zA-Z\-]+\.[a-zA-Z\-]+)\)", _text(primary_match.group(1)))
        primary = found.group(1) if found else None
    if primary:
        # arXiv 明确标了主分类就用它，别拿「列表里的第一个」当主分类
        categories = [primary] + [c for c in categories if c != primary]

    abstract_match = re.search(r"<p[^>]*class=['\"][^'\"]*mathjax[^'\"]*['\"][^>]*>(.*?)</p>",
                               dd_html, re.S | re.I)
    abstract = _collapse(_text(abstract_match.group(1))) if abstract_match else None

    return {
        "arxiv_id": arxiv_id,
        "title": title,
        "abstract": abstract,
        "authors": authors,
        "published": None,
        "updated": None,
        "year": None,
        "categories": categories,
        "primary_category": primary or (categories[0] if categories else None),
        "doi": None,
        "url": ABS_URL_TEMPLATE.format(arxiv_id=arxiv_id),
        "pdf_url": PDF_URL_TEMPLATE.format(arxiv_id=arxiv_id),
        "announce_type": None,  # 由调用方按所在分段填
    }


def parse_listing(html: str) -> tuple[list[dict[str, Any]], str | None, bool]:
    """解析一页，返回 (条目, 公告日期原文, 是否还有没取完的)。

    条目只含 new + cross。段头写着本页几条、总共几条，两处都用上：
    解析出的条数对不上本页声明 → 抛错（结构变了）；本页声明 < 总数 → 还要翻页。
    """
    spans = _section_spans(html)
    if not spans:
        raise ArxivListingError("列表页里找不到任何分段标题（页面结构可能变了）")

    date_match = _HEADER_DATE_RE.search(html)
    announced = date_match.group(1) if date_match else None

    entries: list[dict[str, Any]] = []
    incomplete = False
    for name, shown, total, start, end in spans:
        if name not in _WANTED_SECTIONS:
            continue
        if shown < total:
            incomplete = True
        parsed = [
            entry
            for match in _ENTRY_RE.finditer(html[start:end])
            if (entry := _parse_entry(match.group("dt"), match.group("dd"))) is not None
        ]
        # 解析不出来必须炸。静默少收和「今天没公告」在下游长得一模一样，
        # 而后者是完全正常的一天——分不开就等于这类故障永远不会被发现。
        if len(parsed) != shown:
            raise ArxivListingError(
                f"{name} 段声明 {shown} 条，实际解析出 {len(parsed)} 条（页面结构可能变了）"
            )
        for entry in parsed:
            entry["announce_type"] = name
        entries.extend(parsed)
    return entries, announced, incomplete
