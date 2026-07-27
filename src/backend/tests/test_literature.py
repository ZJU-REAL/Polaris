"""文献源客户端单测：respx mock HTTP，fakeredis 缓存；零真实网络。"""

import fakeredis.aioredis
import httpx
import pytest
import pytest_asyncio
import respx

from app.services.literature.arxiv import (
    ArxivClient,
    ArxivRateLimitedError,
    build_search_query,
    normalize_arxiv_id,
)
from app.services.literature.cache import MinIntervalLimiter
from app.services.literature.openalex import OpenAlexClient
from app.services.literature.semantic_scholar import SemanticScholarClient

ARXIV_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">
  <entry>
    <id>http://arxiv.org/abs/2406.00001v2</id>
    <title>Autonomous Research  Agents</title>
    <summary>We study autonomous research agents built on LLMs.</summary>
    <published>2026-06-01T00:00:00Z</published>
    <updated>2026-06-02T00:00:00Z</updated>
    <author><name>Alice</name></author>
    <author><name>Bob</name></author>
    <category term="cs.LG"/>
    <category term="cs.AI"/>
    <arxiv:doi>10.1000/agents</arxiv:doi>
  </entry>
  <entry>
    <id>http://arxiv.org/abs/2406.00002v1</id>
    <title>LLM Scientist Benchmark</title>
    <summary>A benchmark for LLM scientists.</summary>
    <published>2026-05-20T00:00:00Z</published>
    <updated>2026-05-20T00:00:00Z</updated>
    <author><name>Carol</name></author>
    <category term="cs.CL"/>
  </entry>
</feed>
"""


ARXIV_RSS = """<?xml version='1.0' encoding='UTF-8'?>
<rss xmlns:arxiv="http://arxiv.org/schemas/atom" xmlns:dc="http://purl.org/dc/elements/1.1/" \
xmlns:atom="http://www.w3.org/2005/Atom" version="2.0">
  <channel>
    <title>cs.CL updates on arXiv.org</title>
    <item>
      <title>Computer-Use Agents at Scale</title>
      <link>https://arxiv.org/abs/2607.10001</link>
      <description>arXiv:2607.10001v1 Announce Type: new
Abstract: We study computer use agents operating at scale with strong results.</description>
      <guid isPermaLink="false">oai:arXiv.org:2607.10001v1</guid>
      <category>cs.CL</category>
      <category>cs.AI</category>
      <pubDate>Mon, 20 Jul 2026 00:00:00 -0400</pubDate>
      <arxiv:announce_type>new</arxiv:announce_type>
      <dc:creator>Alice Smith, Bob Jones</dc:creator>
    </item>
    <item>
      <title>Cross-Listed Planning Methods</title>
      <link>https://arxiv.org/abs/2607.10002</link>
      <description>arXiv:2607.10002v2 Announce Type: cross
Abstract: Planning methods cross-listed from another category.</description>
      <guid isPermaLink="false">oai:arXiv.org:2607.10002v2</guid>
      <category>cs.CL</category>
      <pubDate>Mon, 20 Jul 2026 00:00:00 -0400</pubDate>
      <arxiv:announce_type>cross</arxiv:announce_type>
      <dc:creator>Carol Lee</dc:creator>
    </item>
    <item>
      <title>Old Paper Revised</title>
      <link>https://arxiv.org/abs/2601.09999</link>
      <description>arXiv:2601.09999v3 Announce Type: replace
Abstract: A revised version of an older paper.</description>
      <guid isPermaLink="false">oai:arXiv.org:2601.09999v3</guid>
      <category>cs.CL</category>
      <pubDate>Mon, 20 Jul 2026 00:00:00 -0400</pubDate>
      <arxiv:announce_type>replace</arxiv:announce_type>
      <dc:creator>Dave Kim</dc:creator>
    </item>
    <item>
      <title>Another Old Cross Revision</title>
      <link>https://arxiv.org/abs/2601.08888</link>
      <description>arXiv:2601.08888v2 Announce Type: replace-cross
Abstract: A revised cross-listed older paper.</description>
      <guid isPermaLink="false">oai:arXiv.org:2601.08888v2</guid>
      <category>cs.CL</category>
      <pubDate>Mon, 20 Jul 2026 00:00:00 -0400</pubDate>
      <arxiv:announce_type>replace-cross</arxiv:announce_type>
      <dc:creator>Eve Wang</dc:creator>
    </item>
  </channel>
</rss>
"""


@pytest_asyncio.fixture
async def cache_redis():
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield redis
    await redis.aclose()


def test_normalize_arxiv_id_and_query():
    assert normalize_arxiv_id("http://arxiv.org/abs/2406.00001v2") == "2406.00001"
    assert normalize_arxiv_id("2406.00001v3") == "2406.00001"
    q = build_search_query(["cs.LG", "cs.AI"], ["research agent"])
    assert q == '(cat:cs.LG OR cat:cs.AI) AND (all:"research agent")'


@respx.mock
async def test_arxiv_search_parses_and_caches(cache_redis):
    route = respx.get(url__regex=r"https://export\.arxiv\.org/api/query.*").mock(
        return_value=httpx.Response(200, text=ARXIV_FEED)
    )
    client = ArxivClient(redis=cache_redis, min_interval=0)
    results = await client.search(categories=["cs.LG"], keywords=["agent"], limit=10)
    assert len(results) == 2
    first = results[0]
    assert first["arxiv_id"] == "2406.00001"
    assert first["title"] == "Autonomous Research Agents"  # 多空白折叠
    assert first["authors"] == [{"name": "Alice"}, {"name": "Bob"}]
    assert first["year"] == 2026
    assert first["doi"] == "10.1000/agents"
    assert first["pdf_url"].endswith("/pdf/2406.00001")

    # 相同参数第二次调用命中 Redis 缓存，不再发 HTTP
    again = await client.search(categories=["cs.LG"], keywords=["agent"], limit=10)
    assert len(again) == 2
    assert route.call_count == 1
    await client.aclose()


@respx.mock
async def test_arxiv_fetch_new_rss_parses_filters_and_caches(cache_redis):
    route = respx.get(url__regex=r"https://rss\.arxiv\.org/rss/cs\.CL").mock(
        return_value=httpx.Response(200, text=ARXIV_RSS)
    )
    client = ArxivClient(redis=cache_redis, min_interval=0)
    entries = await client.fetch_new("cs.CL")

    # 只留 announce_type ∈ {new, cross}；replace / replace-cross（旧论文更新）被跳过
    assert [e["announce_type"] for e in entries] == ["new", "cross"]
    first = entries[0]
    assert first["arxiv_id"] == "2607.10001"  # guid 的 v1 版本号被 normalize 去掉
    assert first["title"] == "Computer-Use Agents at Scale"
    # description "Abstract:" 之后截取为摘要
    assert first["abstract"] == (
        "We study computer use agents operating at scale with strong results."
    )
    assert first["authors"] == [{"name": "Alice Smith"}, {"name": "Bob Jones"}]
    assert first["categories"] == ["cs.CL", "cs.AI"]
    assert first["primary_category"] == "cs.CL"
    assert first["year"] == 2026
    assert first["published"].startswith("2026-07-20")
    assert first["pdf_url"].endswith("/pdf/2607.10001")
    assert first["doi"] is None
    assert entries[1]["arxiv_id"] == "2607.10002"  # v2 也被去版本号

    # 短 TTL 缓存命中：相同分类第二次调用不再发 HTTP
    again = await client.fetch_new("cs.CL")
    assert [e["arxiv_id"] for e in again] == ["2607.10001", "2607.10002"]
    assert route.call_count == 1
    await client.aclose()


@respx.mock
async def test_arxiv_fetch_new_network_error_returns_empty(cache_redis):
    route = respx.get(url__regex=r"https://rss\.arxiv\.org/rss/.*").mock(
        return_value=httpx.Response(503)
    )
    client = ArxivClient(redis=cache_redis, min_interval=0, backoff_base=0.0, max_retries=2)
    assert await client.fetch_new("cs.CL") == []  # 失败容错，不抛
    assert route.call_count == 2  # 503 会重试，耗尽后才吞成 []
    await client.aclose()


# ---- 限流重试（arXiv 429/403）----


@respx.mock
async def test_arxiv_retries_on_429_then_succeeds(cache_redis):
    """429 带 Retry-After（整数秒）→ 退避后重试 → 拿到正常结果。"""
    route = respx.get(url__regex=r"https://export\.arxiv\.org/api/query.*").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "0"}),
            httpx.Response(200, text=ARXIV_FEED),
        ]
    )
    client = ArxivClient(redis=cache_redis, min_interval=0, backoff_base=0.0)
    results = await client.search(categories=["cs.LG"], limit=10)
    assert len(results) == 2
    assert route.call_count == 2
    await client.aclose()


@respx.mock
async def test_arxiv_retry_after_accepts_http_date(cache_redis):
    """Retry-After 也可能是 HTTP-date；过去的时间点等价于「立刻可重试」。"""
    route = respx.get(url__regex=r"https://export\.arxiv\.org/api/query.*").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT"}),
            httpx.Response(200, text=ARXIV_FEED),
        ]
    )
    client = ArxivClient(redis=cache_redis, min_interval=0, backoff_base=0.0)
    assert len(await client.search(categories=["cs.LG"], limit=10)) == 2
    assert route.call_count == 2
    await client.aclose()


@respx.mock
async def test_arxiv_403_is_retried(cache_redis):
    """arXiv 对它认为过量的客户端历史上返 403 而非 429——当成永久失败会白丢整批论文。"""
    route = respx.get(url__regex=r"https://export\.arxiv\.org/api/query.*").mock(
        side_effect=[httpx.Response(403), httpx.Response(200, text=ARXIV_FEED)]
    )
    client = ArxivClient(redis=cache_redis, min_interval=0, backoff_base=0.0)
    assert len(await client.search(categories=["cs.LG"], limit=10)) == 2
    assert route.call_count == 2
    await client.aclose()


@respx.mock
async def test_arxiv_raises_after_exhausting_retries(cache_redis):
    route = respx.get(url__regex=r"https://export\.arxiv\.org/api/query.*").mock(
        return_value=httpx.Response(429)
    )
    client = ArxivClient(redis=cache_redis, min_interval=0, backoff_base=0.0, max_retries=3)
    with pytest.raises(ArxivRateLimitedError):
        await client.search(categories=["cs.LG"], limit=10)
    assert route.call_count == 3
    await client.aclose()


@respx.mock
async def test_arxiv_client_error_is_not_retried(cache_redis):
    """400（检索式写错）要立刻失败，不能烧四次退避去撞同一堵墙。"""
    route = respx.get(url__regex=r"https://export\.arxiv\.org/api/query.*").mock(
        return_value=httpx.Response(400)
    )
    client = ArxivClient(redis=cache_redis, min_interval=0, backoff_base=0.0)
    with pytest.raises(httpx.HTTPStatusError):
        await client.search(categories=["cs.LG"], limit=10)
    assert route.call_count == 1
    await client.aclose()


@respx.mock
async def test_arxiv_retries_on_read_timeout(cache_redis):
    """生产上见过 ReadTimeout 让整个建库任务停在第一步——超时同样要重试。"""
    route = respx.get(url__regex=r"https://export\.arxiv\.org/api/query.*").mock(
        side_effect=[httpx.ReadTimeout("timed out"), httpx.Response(200, text=ARXIV_FEED)]
    )
    client = ArxivClient(redis=cache_redis, min_interval=0, backoff_base=0.0)
    assert len(await client.search(categories=["cs.LG"], limit=10)) == 2
    assert route.call_count == 2
    await client.aclose()


@respx.mock
async def test_download_pdf_retries_on_429(cache_redis):
    """PDF 下载是 arXiv 流量的大头，必须同样享受重试。"""
    route = respx.get(url__regex=r"https://arxiv\.org/pdf/.*").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "0"}),
            httpx.Response(200, content=b"%PDF-1.7 stub"),
        ]
    )
    client = ArxivClient(redis=cache_redis, min_interval=0, backoff_base=0.0)
    assert await client.download_pdf("2406.00001") == b"%PDF-1.7 stub"
    assert route.call_count == 2
    await client.aclose()


async def test_min_interval_limiter_penalize():
    """penalize 把惩罚摊给共享限流器的所有协程；interval=0 时整体关闭，是 no-op。"""
    limiter = MinIntervalLimiter(0.05)
    await limiter.acquire()
    before = limiter._last
    await limiter.penalize(5.0)
    assert limiter._last >= before + 4.9

    off = MinIntervalLimiter(0)
    await off.penalize(5.0)
    assert off._last == 0.0


@respx.mock
async def test_s2_references_with_429_backoff(cache_redis):
    payload = {
        "data": [
            {
                "citedPaper": {
                    "paperId": "abc123",
                    "title": "Cited Paper",
                    "abstract": "About agents.",
                    "year": 2025,
                    "venue": "NeurIPS",
                    "externalIds": {"ArXiv": "2405.00004", "DOI": "10.1/cited"},
                    "authors": [{"name": "Dave"}],
                }
            },
            {"citedPaper": None},
        ]
    }
    route = respx.get(
        url__regex=r"https://api\.semanticscholar\.org/graph/v1/paper/arXiv:2404\.11111/references.*"
    ).mock(
        side_effect=[
            httpx.Response(429),
            httpx.Response(200, json=payload),
        ]
    )
    client = SemanticScholarClient(redis=cache_redis, api_key="", rate=10_000, backoff_base=0.0)
    refs = await client.get_references("arXiv:2404.11111")
    assert route.call_count == 2  # 429 → 退避重试成功
    assert len(refs) == 1
    assert refs[0]["externalIds"]["ArXiv"] == "2405.00004"

    # 缓存命中
    await client.get_references("arXiv:2404.11111")
    assert route.call_count == 2
    await client.aclose()


@respx.mock
async def test_openalex_by_arxiv_id(cache_redis):
    work = {
        "id": "https://openalex.org/W1",
        "title": "Anchor Paper",
        "doi": "https://doi.org/10.48550/arXiv.2404.11111",
        "publication_year": 2024,
        "cited_by_count": 42,
        "primary_location": {"source": {"display_name": "arXiv"}},
        "authorships": [{"author": {"display_name": "Eve"}}],
    }
    respx.get(
        url__regex=r"https://api\.openalex\.org/works/doi:10\.48550/arXiv\.2404\.11111.*"
    ).mock(return_value=httpx.Response(200, json=work))
    client = OpenAlexClient(redis=cache_redis, mailto="test@example.org")
    meta = await client.get_by_arxiv("2404.11111")
    assert meta is not None
    assert meta["cited_by_count"] == 42
    assert meta["doi"] == "10.48550/arXiv.2404.11111"
    assert meta["authors"] == [{"name": "Eve", "affiliations": []}]
    assert meta["venue"] == "arXiv"
    await client.aclose()


@respx.mock
async def test_openalex_404_returns_none(cache_redis):
    respx.get(url__regex=r"https://api\.openalex\.org/works/.*").mock(
        return_value=httpx.Response(404)
    )
    client = OpenAlexClient(redis=cache_redis, mailto="test@example.org")
    assert await client.get_by_doi("10.1/nope") is None
    await client.aclose()


# ---- 抽取文本清洗（postgres UTF8 拒绝 0x00，见 wiki.fetch_extract 失败案例） ----


def test_sanitize_text_strips_nul_and_control_chars():
    from app.services.literature.pdf_extract import sanitize_text

    dirty = "abc\x00def\x01\x02\r\nline2\rline3\tkeep\n\npara"
    clean = sanitize_text(dirty)
    assert "\x00" not in clean
    assert "\x01" not in clean and "\x02" not in clean
    assert "line2\nline3\tkeep" in clean  # \n \t 保留，\r 统一成 \n


def test_sanitize_text_drops_lone_surrogates():
    from app.services.literature.pdf_extract import sanitize_text

    clean = sanitize_text("ok\ud800tail")
    assert clean == "oktail"
    clean.encode("utf-8")  # 不再抛 UnicodeEncodeError


def test_split_text_after_sanitize_has_no_nul():
    from app.services.chunks import split_text
    from app.services.literature.pdf_extract import sanitize_text

    chunks = split_text(sanitize_text("para1 with \x00 nul\n\npara2" * 100))
    assert chunks and all("\x00" not in c for c in chunks)
