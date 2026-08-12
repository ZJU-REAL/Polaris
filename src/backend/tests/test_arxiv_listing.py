"""解析 arXiv 每日公告列表页。

样本是从 2026-08-12 真实的 ``/list/cs.AI/new`` 裁下来的：页头、三个分段标题、每段两条
真实条目，标记与嵌套一字未改。手写的假 HTML 只能验证「我以为页面长什么样」，
而这个解析器唯一的风险就是我以为的和实际不一样。
"""

from pathlib import Path

import pytest

from app.services.literature.arxiv_listing import ArxivListingError, parse_listing

SAMPLE = (Path(__file__).parent / "fixtures" / "arxiv_listing_sample.html").read_text()


def test_parses_real_markup_into_entries():
    entries, announced, incomplete = parse_listing(SAMPLE)

    assert announced == "12 August 2026", "公告日期取页面自己写的，不取 RSS 那个会滞后的"
    assert incomplete is False
    assert len(entries) == 4

    first = entries[0]
    assert first["arxiv_id"] == "2608.09949"
    assert first["title"] == "Closed-Loop LLM Co-Pilots for Digital Agriculture"
    # 标题和作者之间没有任何标签词，按纯文本切会把作者名粘进标题且不报错
    assert "Kernbach" not in first["title"]
    assert [a["name"] for a in first["authors"]] == ["Serge Kernbach"]
    assert first["primary_category"] == "cs.AI"
    assert "physics.bio-ph" in first["categories"]
    assert first["abstract"] and first["abstract"].startswith("This study evaluates")
    assert first["url"].endswith("2608.09949")


def test_replacements_are_left_out():
    """Replacement 是旧论文更新，不是当天新公告。"""
    entries, _, _ = parse_listing(SAMPLE)
    kinds = {e["announce_type"] for e in entries}
    assert kinds == {"new", "cross"}


def test_count_mismatch_raises_instead_of_returning_less():
    """段头说 2 条、只解析出 1 条 → 抛错。

    这是换掉 RSS 的全部理由。少收和「今天没公告」在下游长得一模一样，而后者是完全
    正常的一天；分不开，这类故障就永远不会被发现——2026-08-12 漏掉一整天正是如此。
    """
    # 模拟 arXiv 改了类名——这正是 HTML 解析真实会遇到的失效方式
    broken = SAMPLE.replace("list-title", "list-heading", 1)
    with pytest.raises(ArxivListingError, match="解析出"):
        parse_listing(broken)


def test_missing_sections_raise():
    """页面完全变样时不能返回空——空是「今天没论文」的意思。"""
    with pytest.raises(ArxivListingError):
        parse_listing("<html><body>arXiv is down for maintenance</body></html>")


def test_truncated_page_reports_incomplete():
    """段头写 first N of M 时要说「还没取完」，让上层去翻页。"""
    truncated = SAMPLE.replace(
        "<h3>New submissions (showing 2 of 2 entries)</h3>",
        "<h3>New submissions (showing first 2 of 90 entries)</h3>",
    )
    entries, _, incomplete = parse_listing(truncated)
    assert incomplete is True
    assert len(entries) == 4, "本页解析出的照常返回，翻页交给调用方"
