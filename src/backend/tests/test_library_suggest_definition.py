"""AI 生成收录设置（services/libraries.suggest_definition + POST /libraries/suggest-definition）。

覆盖：服务解析（合法 JSON → 结构化 / 坏 JSON、缺字段、调用失败 → 结构完整空兜底 /
字段强制与去重截断）；端点（happy path 返回 keywords/rubric/anchors；坏 JSON 仍 200
返回空兜底；未登录 401；大模型权限 blocked → 403）。
"""

import json
from types import SimpleNamespace

from sqlalchemy import select

from app.core.db import get_sessionmaker
from app.models.user import User
from app.services import libraries as libraries_service
from tests.conftest import register_and_login

GOOD_JSON = json.dumps(
    {
        "keywords": {
            "arxiv_categories": ["cs.CL", "cs.AI", "cs.CL", 42],  # 去重 + 丢非字符串
            "include": [" retrieval-augmented generation ", "in-context learning"],
        },
        "rubric": [
            {"name": "任务相关性", "description": "直接研究该任务得高分", "weight": 0.6},
            {"name": "方法新颖性", "weight": "1.8"},  # weight 解析成 float 并夹到 1.0；desc 兜空
            {"description": "没有 name 的项会被丢弃", "weight": 0.2},
        ],
        "anchors": [
            {"title": "RAG for Knowledge Tasks", "arxiv_id": "2005.11401", "reason": "奠基工作"},
            {"title": "No Arxiv Anchor", "reason": "arxiv 可空"},
            {"reason": "没有 title 的项会被丢弃"},
        ],
    },
    ensure_ascii=False,
)


class _StubLLM:
    """记录调用参数的假路由器；error 给定时抛出（测调用失败兜底）。"""

    def __init__(self, content: str = "{}", error: Exception | None = None):
        self.content = content
        self.error = error
        self.calls: list[dict] = []

    async def complete(self, stage, messages, **kwargs):
        self.calls.append({"stage": stage, "messages": messages, **kwargs})
        if self.error is not None:
            raise self.error
        return SimpleNamespace(content=self.content)


# ---- 服务函数单测（无 DB / 无 HTTP） ----


async def test_suggest_definition_parses_and_coerces():
    llm = _StubLLM(GOOD_JSON)
    result = await libraries_service.suggest_definition(
        name="检索增强生成", statement="用外部知识增强 LLM 生成", llm=llm
    )
    assert result["keywords"]["arxiv_categories"] == ["cs.CL", "cs.AI"]  # 去重、丢非字符串
    assert result["keywords"]["include"] == [
        "retrieval-augmented generation",  # strip
        "in-context learning",
    ]
    rubric = result["rubric"]
    assert [r["name"] for r in rubric] == ["任务相关性", "方法新颖性"]  # 无 name 项被丢
    assert rubric[0]["weight"] == 0.6
    assert rubric[1]["weight"] == 1.0 and rubric[1]["description"] == ""  # 夹到 1.0，desc 兜空
    anchors = result["anchors"]
    assert [a["title"] for a in anchors] == ["RAG for Knowledge Tasks", "No Arxiv Anchor"]
    assert anchors[0]["arxiv_id"] == "2005.11401"
    assert anchors[1]["arxiv_id"] is None  # arxiv 可空
    # 调用参数：stage / 记账 / prompt 内容
    call = llm.calls[0]
    assert call["stage"] == "librarian"
    assert call["user_id"] is None
    assert "检索增强生成" in call["messages"][1].content


async def test_suggest_definition_bad_json_returns_empty():
    for content in ("解析不了，抱歉", "[]", "not json {", '{"keywords": "not a dict"}'):
        result = await libraries_service.suggest_definition(
            name="X", statement="Y", llm=_StubLLM(content)
        )
        assert result == {
            "keywords": {"arxiv_categories": [], "include": []},
            "rubric": [],
            "anchors": [],
        }


async def test_suggest_definition_missing_fields_are_structurally_complete():
    # 合法 JSON 但只给了部分字段 → 缺的字段补空列表，结构完整
    result = await libraries_service.suggest_definition(
        name="X", statement="", llm=_StubLLM('{"keywords": {"include": ["a"]}}')
    )
    assert result == {
        "keywords": {"arxiv_categories": [], "include": ["a"]},
        "rubric": [],
        "anchors": [],
    }


async def test_suggest_definition_llm_error_returns_empty():
    llm = _StubLLM(error=RuntimeError("boom"))
    result = await libraries_service.suggest_definition(name="X", statement="Y", llm=llm)
    assert result == {
        "keywords": {"arxiv_categories": [], "include": []},
        "rubric": [],
        "anchors": [],
    }


# ---- 端点接线（POST /libraries/suggest-definition） ----


async def _set_llm_access(email: str, level: str) -> None:
    async with get_sessionmaker()() as session:
        user = (await session.execute(select(User).where(User.email == email))).scalar_one()
        user.llm_access = level
        await session.commit()


async def test_suggest_endpoint_happy_path(client, monkeypatch):
    import app.api.libraries as libraries_api

    monkeypatch.setattr(libraries_api, "get_llm_router", lambda: _StubLLM(GOOD_JSON))
    token = await register_and_login(client)
    resp = await client.post(
        "/api/libraries/suggest-definition",
        json={"name": "检索增强生成", "statement": "用外部知识增强 LLM 生成"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["keywords"]["arxiv_categories"] == ["cs.CL", "cs.AI"]
    assert body["keywords"]["include"][0] == "retrieval-augmented generation"
    assert [r["name"] for r in body["rubric"]] == ["任务相关性", "方法新颖性"]
    assert body["anchors"][0]["arxiv_id"] == "2005.11401"
    assert body["anchors"][1]["arxiv_id"] is None


async def test_suggest_endpoint_bad_json_returns_empty_200(client, monkeypatch):
    import app.api.libraries as libraries_api

    monkeypatch.setattr(libraries_api, "get_llm_router", lambda: _StubLLM("对不起，我做不到"))
    token = await register_and_login(client)
    resp = await client.post(
        "/api/libraries/suggest-definition",
        json={"name": "X", "statement": "Y"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "keywords": {"arxiv_categories": [], "include": []},
        "rubric": [],
        "anchors": [],
    }


async def test_suggest_endpoint_requires_auth(client):
    resp = await client.post(
        "/api/libraries/suggest-definition", json={"name": "X", "statement": "Y"}
    )
    assert resp.status_code == 401


async def test_suggest_endpoint_blocked_llm_access_forbidden(client, monkeypatch):
    import app.api.libraries as libraries_api

    monkeypatch.setattr(libraries_api, "get_llm_router", lambda: _StubLLM(GOOD_JSON))
    email = "blocked@example.com"
    token = await register_and_login(client, email=email)
    await _set_llm_access(email, "blocked")
    resp = await client.post(
        "/api/libraries/suggest-definition",
        json={"name": "X", "statement": "Y"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403
    assert resp.json()["detail"] == "LLM_ACCESS_BLOCKED"
