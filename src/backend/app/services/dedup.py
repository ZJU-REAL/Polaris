"""跨库去重键（全平台共享工具）。

层级：arxiv → doi → 规范化标题 sha1（优先序可由调用方覆盖——发表记录历史键是 doi 优先，
必须保持前缀顺序不变，否则已存 dedup_key 全部失配）。

标题层级只在没有 arxiv/doi 时启用；内容池（papers.dedup_key）在标题哈希里
额外掺入年份 + 规范化首作者（RFC §4.3：降低「不同论文标题撞车」的误合并概率），
个人库 / 发表记录沿用纯标题哈希（兼容既有数据）。
"""

import hashlib
import re
import unicodedata
from dataclasses import dataclass
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


first_author_name = _first_author_name


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


class PoolKey(str):
    """内容池去重键，顺带记着算出它的那份标题 / 年份 / 作者 / arXiv 号。

    键本身是哈希，查不到就无从知道「这篇和池里哪篇只差一个标题写法」。把原始信息
    挂在键上，``libraries.find_pool_paper`` 在硬键全没命中时就能再按
    :func:`same_work` 找一遍——所有「先算键、再查池」的写路径不用改调用方式就都
    带上这一层。当普通 ``str`` 用（存库、比较）完全一样。
    """

    title: str | None
    year: int | None
    authors: list[Any] | None
    arxiv_id: str | None

    def __new__(
        cls,
        value: str,
        *,
        title: str | None = None,
        year: int | None = None,
        authors: list[Any] | None = None,
        arxiv_id: str | None = None,
    ) -> "PoolKey":
        key = super().__new__(cls, value)
        key.title = title
        key.year = year
        key.authors = authors
        key.arxiv_id = arxiv_id
        return key


def pool_dedup_key(
    *,
    arxiv_id: str | None,
    doi: str | None,
    title: str,
    year: int | None = None,
    authors: list[Any] | None = None,
) -> PoolKey:
    """内容池（papers.dedup_key）口径：arxiv → doi → 标题+年份+首作者哈希。"""
    key = dedup_key_for(arxiv_id=arxiv_id, doi=doi, title=title, year=year, authors=authors)
    assert key is not None  # title 非空保证有键
    return PoolKey(key, title=title, year=year, authors=authors, arxiv_id=arxiv_id)


# ---- 「同一篇论文、标题写法不同」的判定（入库查重与库内合并共用） ----
#
# 实际撞见的例子：arXiv 给的是 "REALM: Retrieval-Augmented Language Model
# Pre-Training"，OpenAlex 收的会议版是 "Retrieval Augmented Language Model
# Pre-Training"，后者既没有 arXiv 号也没有 DOI——标识对不上，标题哈希也对不上，
# 同一篇论文在一个库里进了两次。

# 标题开头的「代号：」——单个不含空白的词（REALM、RoBERTa、GPT-3、T5），后面跟冒号
# 或两侧带空格的破折号
_LEADING_NAME = re.compile(r"^\s*([^\s:：—–]{1,24})\s*(?:[:：]|\s[-—–]\s)\s*(\S.*)$")
# 去掉代号之后剩下的部分至少要这么长，才敢拿它单独当标题比
_MIN_CORE_WORDS = 3
_MIN_TITLE_CHARS = 10


def _fold(text: str) -> list[str]:
    """小写、去重音、标点和连字符都当分隔，返回词列表。"""
    plain = "".join(
        ch for ch in unicodedata.normalize("NFKD", text.casefold()) if not unicodedata.combining(ch)
    )
    return [t for t in re.split(r"[\W_]+", plain) if t]


def title_keys(title: str | None) -> tuple[str, str | None]:
    """标题的比对键：(整题, 去掉开头代号后的部分或 None)。

    键是把词直接连起来（不留空格），所以大小写、标点、``Pre-Training`` /
    ``Pre Training`` / ``Pretraining`` 这几种写法都落到同一个键上。
    """
    if not title:
        return "", None
    full = "".join(_fold(title))
    core: str | None = None
    if m := _LEADING_NAME.match(title):
        words = _fold(m.group(2))
        joined = "".join(words)
        if len(words) >= _MIN_CORE_WORDS and len(joined) >= _MIN_TITLE_CHARS:
            core = joined
    return full, core


def titles_match(a: str | None, b: str | None) -> bool:
    """两个标题是不是同一个题目的不同写法。

    整题相同，或者一边是另一边去掉开头代号后的样子。**不**拿两边各自去掉代号后的
    部分互相比：``BERT: X`` 和 ``RoBERTa: X`` 是两篇论文。也不做近似匹配——
    ``X`` 和 ``X II``、``... v2`` 只差一两个字符，却是不同的论文。
    """
    full_a, core_a = title_keys(a)
    full_b, core_b = title_keys(b)
    if len(full_a) < _MIN_TITLE_CHARS or len(full_b) < _MIN_TITLE_CHARS:
        return False
    return full_a in (full_b, core_b) or core_a == full_b


def first_author_surname(authors: list[Any] | None) -> tuple[str, frozenset[str]] | None:
    """首作者的 (姓, 名字里的全部词)；没有作者返回 None。

    ``Guu, Kelvin`` 取逗号前，``Kelvin Guu`` / ``K. Guu`` 取最后一个词。
    """
    name = _first_author_name(authors)
    if not name:
        return None
    head, comma, _ = name.partition(",")
    words = _fold(name)
    if not words:
        return None
    surname = " ".join(_fold(head)) if comma and _fold(head) else words[-1]
    return surname, frozenset(words)


def _same_first_author(a: list[Any] | None, b: list[Any] | None) -> bool:
    left, right = first_author_surname(a), first_author_surname(b)
    if left is None or right is None:
        return False
    if left[0] == right[0]:
        return True
    # 「Wei Zhang」/「Zhang Wei」：姓名顺序不同但是同一组词（缩写的单字母不算数）
    return len(left[1]) > 1 and left[1] == right[1] and all(len(w) > 1 for w in left[1])


def _bare_arxiv(arxiv_id: str | None) -> str:
    return re.sub(r"v\d+$", "", (arxiv_id or "").strip().lower())


@dataclass(frozen=True)
class WorkRef:
    """判定「是不是同一篇」要用到的那几项。"""

    title: str | None
    year: int | None
    authors: list[Any] | None
    arxiv_id: str | None = None

    @property
    def bare_arxiv(self) -> str:
        """arXiv 号去掉版本后缀、转小写（没有为空串）。"""
        return _bare_arxiv(self.arxiv_id)


def same_work(a: WorkRef, b: WorkRef) -> bool:
    """没有共同标识时，两条记录是不是同一篇论文。三项要同时成立：

    - 标题是同一个题目的不同写法（:func:`titles_match`）；
    - 两边都有年份，且相差不超过 1（预印本和正式发表常常跨年）；
    - 两边都有首作者，且姓相同。

    缺年份或缺作者就不认——宁可留两行等人来合并，也不把两篇不同的论文并成一篇
    （合并删掉的那一行找不回来）。两边各有 arXiv 号且不同，直接判不是同一篇。
    """
    if a.year is None or b.year is None or abs(a.year - b.year) > 1:
        return False
    if a.bare_arxiv and b.bare_arxiv and a.bare_arxiv != b.bare_arxiv:
        return False
    return titles_match(a.title, b.title) and _same_first_author(a.authors, b.authors)
