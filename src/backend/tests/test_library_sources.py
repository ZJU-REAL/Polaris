"""文献库声明自己的检索来源（去 arXiv 化）。

平台的目标是「可插拔学科／领域的通用科研发现」，而建库表单第一个问的是 arXiv 分类
——做结构、做临床的人的领域在 arXiv 上根本没有对应。根因不是文案：入库路径写死了
``require_source("arxiv")``，所以表单只能问它能用的东西。

这些用例钉三件事：
1. 存量库（没配来源）行为一字不变——只走 arXiv；
2. 声明了别的源就真的去检索它们；
3. 某个源不可用时如实报出来，而不是静默少抓。
"""

from app.agents.voyage.actions_wiki import (
    DEFAULT_LIBRARY_SOURCES,
    _configured_sources,
    _entry_from_candidate,
)


def test_a_library_without_sources_still_uses_arxiv_only():
    """存量库的 definition 里没有这个键——行为必须与改动之前逐字节一致。"""
    assert _configured_sources({}) == list(DEFAULT_LIBRARY_SOURCES)
    assert _configured_sources({"sources": []}) == list(DEFAULT_LIBRARY_SOURCES)
    assert _configured_sources({"sources": None}) == list(DEFAULT_LIBRARY_SOURCES)


def test_a_library_can_declare_other_sources():
    """做生物的人订 PubMed，做化学的订 Crossref——不必先有 arXiv 分类。"""
    assert _configured_sources({"sources": ["pubmed", "crossref"]}) == ["pubmed", "crossref"]


def test_arxiv_is_not_forced_in():
    """选了别的源就不该再被塞一个 arXiv 进去：那正是这次改动要去掉的默认。"""
    assert "arxiv" not in _configured_sources({"sources": ["pubmed"]})


def test_unknown_sources_are_dropped_not_fatal():
    """源可能只是暂时没配密钥。整份配置作废等于让人重填。"""
    got = _configured_sources({"sources": ["pubmed", "not-a-source"]})
    assert got == ["pubmed"]


def test_declaration_order_is_kept():
    """顺序是用户写的检索优先级，不该被集合运算打乱。"""
    assert _configured_sources({"sources": ["crossref", "arxiv", "pubmed"]}) == [
        "crossref",
        "arxiv",
        "pubmed",
    ]


# ---- 通用源的候选映射 ----


class _Candidate:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def test_candidate_maps_onto_the_dedup_keys():
    """不映射的话标题能进、去重键全丢——同一篇论文下次检索会再插一遍。"""
    item = _Candidate(
        title="Blast response of composite beams",
        abstract="...",
        authors=["Wei Zhang"],
        year=2026,
        venue="Engineering Structures",
        doi="10.1000/x",
        url="https://example.org/x",
        metadata={"arxiv_id": "2601.00001", "published": "2026-01-02T00:00:00Z"},
    )
    entry = _entry_from_candidate(item)
    assert entry["doi"] == "10.1000/x"
    assert entry["arxiv_id"] == "2601.00001"
    assert entry["title"] == "Blast response of composite beams"
    assert entry["year"] == 2026
    assert entry["primary_category"] == "Engineering Structures"
    assert entry["published"] == "2026-01-02T00:00:00Z"


def test_a_candidate_without_an_arxiv_id_still_maps():
    """PubMed / Crossref 给不出 arxiv_id；那一栏为空是正常的，去重靠 DOI。"""
    item = _Candidate(
        title="A clinical trial",
        abstract=None,
        authors=[],
        year=2025,
        venue="NEJM",
        doi="10.1056/y",
        url=None,
        metadata={},
    )
    entry = _entry_from_candidate(item)
    assert entry["arxiv_id"] is None
    assert entry["doi"] == "10.1056/y"


def test_metadata_absent_does_not_raise():
    """老产物/手工数据可能连 metadata 都没有，映射不该炸。"""
    item = _Candidate(
        title="T", abstract=None, authors=None, year=None, venue=None, doi=None, url=None
    )
    entry = _entry_from_candidate(item)
    assert entry["title"] == "T"
    assert entry["arxiv_id"] is None
