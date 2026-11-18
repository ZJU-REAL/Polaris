"""界面语言：后端拼的给人看的短句（问候语、工具调用的一行摘要）跟着界面语言走。"""

import re

import pytest

from app.core import lang as lang_module
from app.core.lang import iterate_in_lang, resolve_lang, tr, use_lang
from app.services import buddy
from app.tools.registry import get_tool, list_tools
from tests.conftest import register_and_login
from tests.test_chat_agent_api import (  # noqa: F401
    _headers,
    _parse_sse,
    _project_with_paper,
    agent_on,
)

_CJK = re.compile(r"[一-鿿]")


def test_explicit_lang_wins_then_accept_language_then_chinese():
    assert resolve_lang("en", "zh-CN") == "en"
    assert resolve_lang("EN-us", None) == "en"
    assert resolve_lang(None, "en-US,en;q=0.9,zh;q=0.8") == "en"
    assert resolve_lang(None, "zh-CN,zh;q=0.9,en;q=0.8") == "zh"
    assert resolve_lang(None, "fr-FR,en;q=0.5") == "en"  # 认识的里面排最前的
    assert resolve_lang(None, "fr-FR") == "zh" and resolve_lang(None, None) == "zh"
    assert resolve_lang("klingon", "en") == "en"  # 认不出的显式值当作没给


def test_tr_follows_the_language_in_effect_and_resets():
    assert tr("中", "en") == "中"
    with use_lang("en"):
        assert tr("中", "en") == "en"
        with use_lang("zh"):
            assert tr("中", "en") == "中"
        assert tr("中", "en") == "en"
    assert lang_module.current_lang() == "zh"


async def test_iterate_in_lang_sets_the_language_only_while_producing_events():
    seen: list[str] = []

    async def events():
        for _ in range(2):
            seen.append(lang_module.current_lang())
            yield tr("中", "en")

    out = []
    async for item in iterate_in_lang(events(), "en"):
        out.append(item)
        assert lang_module.current_lang() == "zh"  # 消费方这边不受影响
    assert out == ["en", "en"] and seen == ["en", "en"]


def _stats(**kw) -> buddy.BuddyStats:
    base = dict.fromkeys(
        (
            "saved_recent",
            "saved_total",
            "ideas_recent",
            "experiments_running",
            "daily_today",
            "reading_now",
            "manuscripts_active",
            "topics",
        ),
        0,
    )
    return buddy.BuddyStats(**{**base, **kw})


@pytest.mark.parametrize(
    "stats",
    [
        _stats(),
        _stats(experiments_running=3, ideas_recent=5, saved_recent=7),
        _stats(daily_today=384, saved_total=40, reading_now=2, manuscripts_active=1),
    ],
)
def test_english_greeting_opening_and_nudge_have_no_chinese(stats):
    greeting = buddy.compose_greeting(stats, name="Ada", lang="en")
    assert greeting and not _CJK.search(greeting), greeting
    for page in (None, "paper", "idea", "experiment", "manuscript", "library"):
        question, cards = buddy.compose_opening(stats, page_kind=page, lang="en")
        text = question + "".join(c["summary"] + c["prompt"] for c in cards)
        assert not _CJK.search(text), (page, text)
        assert len(cards) == len(buddy.compose_opening(stats, page_kind=page)[1])
    nudge = buddy.compose_nudge(stats, lang="en")
    assert nudge is None or not _CJK.search(nudge)
    # 中文（默认）不变
    assert _CJK.search(buddy.compose_greeting(stats))


def test_english_greeting_reports_the_same_numbers():
    line = buddy.compose_greeting(_stats(experiments_running=3), lang="en")
    assert "3 experiments" in line
    assert "1 experiment " in buddy.compose_greeting(_stats(experiments_running=1), lang="en") + " "


def test_every_tool_summary_has_an_english_form():
    """每个工具的一行摘要：英文界面下不夹中文（内容来自参数的那部分除外）。"""
    checked = 0
    for spec in list_tools():
        if spec.summarize is None:
            continue
        with use_lang("en"):
            line = get_tool(spec.name).summary({}, {})
        assert not _CJK.search(line), (spec.name, line)
        assert spec.summary({}, {})  # 中文（默认）照常出
        checked += 1
    assert checked > 10


async def test_greeting_endpoint_follows_lang_param_and_accept_language(client, agent_on):  # noqa: F811
    token = await register_and_login(client, email="buddy-lang@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    zh = (await client.get("/api/chat/buddy/greeting", headers=headers)).json()
    assert zh["lang"] == "zh" and _CJK.search(zh["greeting"])
    en = (await client.get("/api/chat/buddy/greeting?lang=en", headers=headers)).json()
    text = (
        en["greeting"] + en["question"] + "".join(c["summary"] + c["prompt"] for c in en["cards"])
    )
    assert en["lang"] == "en" and not _CJK.search(text), text
    by_header = (
        await client.get(
            "/api/chat/buddy/greeting", headers={**headers, "Accept-Language": "en-US,en;q=0.9"}
        )
    ).json()
    assert by_header["greeting"] == en["greeting"]
    # 显式参数压过请求头
    forced = (
        await client.get(
            "/api/chat/buddy/greeting?lang=zh", headers={**headers, "Accept-Language": "en"}
        )
    ).json()
    assert forced["greeting"] == zh["greeting"]


async def test_tool_summaries_in_a_turn_follow_the_ui_language(client, agent_on):  # noqa: F811
    headers = await _headers(client, "agent-lang@example.com")
    await _project_with_paper(client, headers, "Tree Search Planning Methods")
    marker = 'POLARIS_FAKE_TOOL:search_papers:{"query": "Tree Search", "mode": "keyword"}'

    async def summary(suffix: str, extra: dict) -> str:
        conv = (await client.post("/api/chat/conversations", json={}, headers=headers)).json()
        async with client.stream(
            "POST",
            f"/api/chat/conversations/{conv['id']}/turn{suffix}",
            json={"question": f"find planning papers\n{marker}"},
            headers={**headers, **extra},
        ) as stream:
            assert stream.status_code == 200
            body = (await stream.aread()).decode()
        results = [d for e, d in _parse_sse(body) if e == "tool_result"]
        assert results and results[0]["ok"] is True
        return results[0]["summary"]

    assert (await summary("", {})).startswith("检索")
    assert (await summary("?lang=en", {})).startswith("Searched")
    assert (await summary("", {"Accept-Language": "en-GB"})).startswith("Searched")
