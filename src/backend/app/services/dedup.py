"""跨库去重键（全平台共享工具）。

层级：arxiv → doi → 规范化标题 sha1（优先序可由调用方覆盖——发表记录历史键是 doi 优先，
必须保持前缀顺序不变，否则已存 dedup_key 全部失配）。

标题层级只在没有 arxiv/doi 时启用；内容池（papers.dedup_key）在标题哈希里
额外掺入年份 + 规范化首作者（RFC §4.3：降低「不同论文标题撞车」的误合并概率），
个人库 / 发表记录沿用纯标题哈希（兼容既有数据）。
"""

import hashlib
import re
from typing import Any

DEFAULT_PRIORITY: tuple[str, ...] = ("arxiv", "doi", "title")


def normalize_title(title: str) -> str:
    """规范化标题（个人库现行算法，全平台基准）：小写 + 非字母数字折叠为空格。"""
    return re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()


def _normalize_author(name: str) -> str:
    return re.sub(r"[^a-z一-鿿0-9]+", " ", name.lower()).strip()


def _first_author_name(authors: list[Any] | None) -> str | None:
    """从 authors JSON（[{"name": ...}] 或 [str]）里取首作者名。"""
    if not authors:
        return None
    first = authors[0]
    name = first.get("name") if isinstance(first, dict) else first
    return name if isinstance(name, str) and name.strip() else None


def dedup_key_for(
    *,
    arxiv_id: str | None = None,
    doi: str | None = None,
    title: str | None = None,
    year: int | None = None,
    authors: list[Any] | None = None,
    priority: tuple[str, ...] = DEFAULT_PRIORITY,
) -> str | None:
    """按优先序生成去重键：``arxiv:<id>`` | ``doi:<小写doi>`` | ``title:<sha1>``。

    - 标题层级要求 title 非空；传入 year/authors 时把「年份 + 首作者」掺进哈希
      （内容池用，防误合并），不传则退化为纯标题哈希（个人库/发表记录兼容口径）。
    - 三层全空时返回 None（调用方自行兜底）。
    """
    for tier in priority:
        if tier == "arxiv" and arxiv_id:
            return f"arxiv:{arxiv_id.lower()}"
        if tier == "doi" and doi:
            return f"doi:{doi.lower()}"
        if tier == "title" and title and title.strip():
            parts = [normalize_title(title)]
            if year is not None:
                parts.append(str(year))
            first_author = _first_author_name(authors)
            if first_author:
                parts.append(_normalize_author(first_author))
            digest = hashlib.sha1("|".join(parts).encode()).hexdigest()
            return f"title:{digest}"
    return None


def pool_dedup_key(
    *,
    arxiv_id: str | None,
    doi: str | None,
    title: str,
    year: int | None = None,
    authors: list[Any] | None = None,
) -> str:
    """内容池（papers.dedup_key）口径：arxiv → doi → 标题+年份+首作者哈希。"""
    key = dedup_key_for(arxiv_id=arxiv_id, doi=doi, title=title, year=year, authors=authors)
    assert key is not None  # title 非空保证有键
    return key
