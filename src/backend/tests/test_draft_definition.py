"""POST /projects/draft-definition：fake LLM 正常起草 + 解析失败/调用失败回退草稿。"""

from app.core.llm.base import CompletionResult
from app.core.llm.router import LLMRouter
from tests.conftest import register_and_login

STATEMENT = "自动化科研 agent 的方法研究"


async def _headers(client) -> dict[str, str]:
    token = await register_and_login(client)
    return {"Authorization": f"Bearer {token}"}


async def test_draft_definition_requires_auth(client):
    resp = await client.post("/api/projects/draft-definition", json={"statement": STATEMENT})
    assert resp.status_code == 401


async def test_draft_definition_llm(client):
    headers = await _headers(client)
    resp = await client.post(
        "/api/projects/draft-definition",
        json={
            "statement": STATEMENT,
            "name": "AutoSci",
            "keywords_include": ["research agent", "llm"],
        },
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["source"] == "llm"
    d = body["definition"]
    assert d["statement"] == STATEMENT  # 原样保留
    assert 2 <= len(d["goals"]) <= 4
    assert d["in_scope"] and d["out_of_scope"]
    assert 3 <= len(d["questions"]) <= 5
    assert 1 <= len(d["rubric"]) <= 3
    for dim in d["rubric"]:
        assert dim["name"] and dim["description"] and dim["weight"] > 0
    kw = d["keywords"]
    assert kw["arxiv_categories"] and all(c.startswith("cs.") for c in kw["arxiv_categories"])
    # 用户关键词保留在前，且可扩充
    assert kw["include"][:2] == ["research agent", "llm"]
    assert len(kw["include"]) >= 2
    assert d["anchor_papers"] == []  # 不编造论文
    assert d["cadence"] == "daily"


async def test_draft_definition_fallback_on_llm_error(client, monkeypatch):
    """router 抛错：重试 2 次（共 3 次调用）后返回规则回退草稿，HTTP 仍 200。"""
    headers = await _headers(client)
    calls = {"n": 0}

    async def boom(self, stage, messages, **kwargs):
        calls["n"] += 1
        raise RuntimeError("llm down")

    monkeypatch.setattr(LLMRouter, "complete", boom)
    resp = await client.post(
        "/api/projects/draft-definition",
        json={"statement": STATEMENT, "keywords_include": ["kw1"]},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["source"] == "fallback"
    assert calls["n"] == 3  # 首次 + 重试 2 次
    d = body["definition"]
    assert d["statement"] == STATEMENT
    assert d["goals"] and d["questions"] and d["rubric"]
    assert d["keywords"]["arxiv_categories"] == ["cs.CL", "cs.AI", "cs.LG"]
    assert d["keywords"]["include"] == ["kw1"]
    assert d["anchor_papers"] == []
    assert d["cadence"] == "daily"


async def test_draft_definition_fallback_on_bad_json(client, monkeypatch):
    """LLM 返回非 JSON：解析失败重试后回退草稿。"""
    headers = await _headers(client)

    async def garbage(self, stage, messages, **kwargs):
        return CompletionResult(content="抱歉，我无法输出 JSON。", model="fake", usage={})

    monkeypatch.setattr(LLMRouter, "complete", garbage)
    resp = await client.post(
        "/api/projects/draft-definition", json={"statement": STATEMENT}, headers=headers
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["source"] == "fallback"
    assert body["definition"]["statement"] == STATEMENT
