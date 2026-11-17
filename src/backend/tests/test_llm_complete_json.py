"""``LLMRouter.complete_json``：结构化输出、一次修复、类型化错误、替身回退。

真实 SDK + 假服务端（tests/test_claude_provider.py 的 Gateway），所以这里看到的
请求体就是会发给 Claude 的请求体。
"""

import json
from typing import Literal

import httpx2
import pytest
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.core.db import get_sessionmaker
from app.core.llm import errors
from app.core.llm.anthropic import NONSTREAM_MAX_TOKENS
from app.core.llm.base import CompletionResult, LLMProvider, Message
from app.core.llm.fake import FakeProvider, minimal_instance
from app.core.llm.router import JSON_REPAIR_MAX_TOKENS, LLMRouter, call_profile
from app.models.llm_config import LLMUsage
from tests.test_claude_provider import MODEL, Gateway, _error, _json, _message, _stream

GATEWAY = {"LITELLM_API_KEY": "sk-gw", "LITELLM_API_BASE": "http://lab.test"}
MESSAGES = [Message("system", "You judge papers."), Message("user", "Judge this one.")]


class Verdict(BaseModel):
    label: Literal["keep", "drop"]
    score: float = Field(ge=0, le=1)
    reason: str = Field(min_length=3)


def _reply(payload: dict | str, **kw) -> httpx2.Response:
    text = payload if isinstance(payload, str) else json.dumps(payload)
    return _json(_message([{"type": "text", "text": text}], **kw))


def _router(claude_settings, gw: Gateway, **provider_kw) -> LLMRouter:
    claude_settings(**GATEWAY)
    router = LLMRouter()
    router.override_provider(gw.provider(**provider_kw))
    return router


GOOD = {"label": "keep", "score": 0.7, "reason": "on topic"}


# ---- 请求体 ----


async def test_request_carries_the_schema_and_keeps_unsupported_constraints_client_side(
    app, claude_settings
):
    gw = Gateway(_reply(GOOD))
    result = await _router(claude_settings, gw).complete_json("relevance", MESSAGES, schema=Verdict)
    assert result.data == Verdict(**GOOD) and result.calls == 1
    body = gw.body
    fmt = body["output_config"]["format"]
    assert fmt["type"] == "json_schema"
    schema = fmt["schema"]
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {"label", "score", "reason"}
    assert schema["properties"]["label"]["enum"] == ["keep", "drop"]
    # 接口不支持数值范围和长度约束：不能出现在发出去的 schema 里（它们由 Pydantic 在本地校验）
    wire = json.dumps(schema)
    assert '"minimum"' not in wire and '"maximum"' not in wire and '"minLength"' not in wire
    # 思考深度跟着环节走，和 format 并排放在 output_config 里
    assert body["output_config"]["effort"] == "low"
    assert body["max_tokens"] == NONSTREAM_MAX_TOKENS
    assert body["thinking"] == {"type": "adaptive"}
    assert "temperature" not in body and "tool_choice" not in body


async def test_cache_prefix_is_the_first_user_block_with_a_breakpoint(app, claude_settings):
    gw = Gateway(_reply(GOOD))
    await _router(claude_settings, gw).complete_json(
        "relevance", MESSAGES, schema=Verdict, cache_prefix="CORPUS " * 50
    )
    content = gw.body["messages"][0]["content"]
    assert content[0] == {
        "type": "text",
        "text": "CORPUS " * 50,
        "cache_control": {"type": "ephemeral"},
    }
    assert content[1] == {"type": "text", "text": "Judge this one."}


async def test_usage_is_recorded_per_model_call(app, claude_settings):
    gw = Gateway(_reply(GOOD))
    result = await _router(claude_settings, gw).complete_json("relevance", MESSAGES, schema=Verdict)
    assert result.usage == {"prompt_tokens": 12, "completion_tokens": 5, "total_tokens": 17}
    assert result.model == MODEL
    async with get_sessionmaker()() as session:
        rows = (await session.execute(select(LLMUsage))).scalars().all()
    assert [(r.stage, r.model, r.prompt_tokens, r.completion_tokens) for r in rows] == [
        ("relevance", MODEL, 12, 5)
    ]


# ---- 修复：截断 ----


async def test_truncation_is_repaired_once_with_a_wider_limit(app, claude_settings):
    gw = Gateway(_reply('{"label": "ke', stop_reason="max_tokens"), _reply(GOOD))
    result = await _router(claude_settings, gw).complete_json(
        "relevance", MESSAGES, schema=Verdict, max_tokens=40
    )
    assert result.data.label == "keep" and result.calls == 2
    first, second = (json.loads(r.content) for r in gw.requests)
    # 调用方给的小上限照发（不像 complete 那样抬到下限），被截断后至少放宽到默认值
    assert first["max_tokens"] == 40
    assert second["max_tokens"] == NONSTREAM_MAX_TOKENS
    assert second["messages"] == first["messages"]  # 同一个问题重问，不带半截输出
    # 两次调用的 token 都算进这次逻辑调用
    assert result.usage["prompt_tokens"] == 24 and result.usage["completion_tokens"] == 10


async def test_truncation_at_the_default_limit_retries_streamed_and_doubled(app, claude_settings):
    from tests.test_claude_provider import _stream_events

    gw = Gateway(
        _reply("{", stop_reason="max_tokens"),
        _stream(_stream_events([{"type": "text", "parts": [json.dumps(GOOD)]}])),
    )
    result = await _router(claude_settings, gw).complete_json("relevance", MESSAGES, schema=Verdict)
    assert result.data.score == 0.7
    second = json.loads(gw.requests[1].content)
    assert second["max_tokens"] == NONSTREAM_MAX_TOKENS * 2 and second["stream"] is True


async def test_still_truncated_after_the_repair_raises_and_stops_at_two_calls(app, claude_settings):
    gw = Gateway(_reply("{", stop_reason="max_tokens"))
    with pytest.raises(errors.LLMTruncatedError) as exc:
        await _router(claude_settings, gw).complete_json(
            "relevance", MESSAGES, schema=Verdict, max_tokens=40
        )
    assert len(gw.requests) == 2
    assert exc.value.transient is False and exc.value.code == "LLM_TRUNCATED"


async def test_truncation_at_the_ceiling_is_not_retried(app, claude_settings):
    from tests.test_claude_provider import _stream_events

    gw = Gateway(
        _stream(_stream_events([{"type": "text", "parts": ["{"]}], stop_reason="max_tokens"))
    )
    with pytest.raises(errors.LLMTruncatedError):
        await _router(claude_settings, gw).complete_json(
            "relevance", MESSAGES, schema=Verdict, max_tokens=JSON_REPAIR_MAX_TOKENS
        )
    assert len(gw.requests) == 1


# ---- 修复：校验 ----


async def test_validation_failure_is_repaired_once_with_the_error_fed_back(app, claude_settings):
    bad = {"label": "keep", "score": 1.7, "reason": "on topic"}
    gw = Gateway(_reply(bad), _reply(GOOD))
    result = await _router(claude_settings, gw).complete_json("relevance", MESSAGES, schema=Verdict)
    assert result.data.score == 0.7 and result.calls == 2
    second = json.loads(gw.requests[1].content)
    assert [m["role"] for m in second["messages"]] == ["user", "assistant", "user"]
    assert json.loads(second["messages"][1]["content"]) == bad
    feedback = second["messages"][2]["content"]
    assert "did not pass validation" in feedback and "score" in feedback
    assert "less than or equal to 1" in feedback
    # 修复重问仍然带着 schema
    assert second["output_config"]["format"]["type"] == "json_schema"


async def test_still_invalid_after_the_repair_raises_and_stops_at_two_calls(app, claude_settings):
    gw = Gateway(_reply({"label": "keep", "score": 5, "reason": "x"}))
    with pytest.raises(errors.LLMSchemaError) as exc:
        await _router(claude_settings, gw).complete_json("relevance", MESSAGES, schema=Verdict)
    assert len(gw.requests) == 2
    assert exc.value.transient is False and exc.value.code == "LLM_SCHEMA"
    assert "Verdict" in str(exc.value)


# ---- 错误归类 ----


@pytest.mark.parametrize(
    ("response", "error_type", "transient", "code"),
    [
        (_error(429, "rate_limit_error"), errors.LLMRateLimitedError, True, "LLM_RATE_LIMITED"),
        (_error(529, "overloaded_error"), errors.LLMOverloadedError, True, "LLM_OVERLOADED"),
        (_error(500, "api_error"), errors.LLMOverloadedError, True, "LLM_OVERLOADED"),
        (_error(401, "authentication_error"), errors.LLMAuthError, False, "LLM_AUTH"),
        (_error(403, "permission_error"), errors.LLMAuthError, False, "LLM_AUTH"),
        (_error(400, "invalid_request_error"), errors.LLMRequestError, False, "LLM_BAD_REQUEST"),
        (_error(404, "not_found_error"), errors.LLMRequestError, False, "LLM_BAD_REQUEST"),
    ],
)
async def test_http_failures_become_typed_errors(
    app, claude_settings, response, error_type, transient, code
):
    gw = Gateway(response)
    with pytest.raises(error_type) as exc:
        await _router(claude_settings, gw).complete_json("relevance", MESSAGES, schema=Verdict)
    assert type(exc.value) is error_type
    assert exc.value.transient is transient and exc.value.code == code
    assert exc.value.user_message and "HTTP" in str(exc.value)
    assert len(gw.requests) == 1  # 这一层不重试；传输层的重试是 SDK 的事
    assert exc.value.as_dict()["transient"] is transient


async def test_the_sdk_retries_transport_level_failures_not_complete_json(app, claude_settings):
    gw = Gateway(_error(529, "overloaded_error"), _reply(GOOD))
    router = _router(claude_settings, gw, max_attempts=2)
    result = await router.complete_json("relevance", MESSAGES, schema=Verdict)
    assert result.calls == 1 and len(gw.requests) == 2


async def test_a_dropped_connection_is_a_transient_transport_error(app, claude_settings):
    class Down(Gateway):
        def __call__(self, request: httpx2.Request) -> httpx2.Response:
            raise httpx2.ConnectError("connection refused", request=request)

    with pytest.raises(errors.LLMTransportError) as exc:
        await _router(claude_settings, Down()).complete_json("relevance", MESSAGES, schema=Verdict)
    assert exc.value.transient is True and exc.value.code == "LLM_UNREACHABLE"


async def test_a_refusal_is_not_retried(app, claude_settings):
    payload = _message([], stop_reason="refusal") | {
        "stop_details": {"type": "refusal", "category": "cyber", "explanation": "declined"}
    }
    gw = Gateway(_json(payload))
    with pytest.raises(errors.LLMRefusalError) as exc:
        await _router(claude_settings, gw).complete_json("relevance", MESSAGES, schema=Verdict)
    assert len(gw.requests) == 1
    assert exc.value.transient is False and exc.value.code == "LLM_REFUSED"
    assert exc.value.category == "cyber"


def test_classify_leaves_unknown_exceptions_alone():
    assert errors.classify(ValueError("a bug in the caller")) is None
    assert errors.classify(NotImplementedError()) is None
    known = errors.LLMAuthError("x")
    assert errors.classify(known) is known
    assert isinstance(errors.classify(TimeoutError()), errors.LLMTransportError)


def test_not_configured_is_a_typed_non_transient_error():
    from app.core.llm.router import LLMNotConfiguredError

    assert LLMNotConfiguredError is errors.LLMNotConfiguredError
    err = LLMNotConfiguredError("no key")
    assert isinstance(err, errors.LLMError) and err.transient is False
    assert err.code == "LLM_NOT_CONFIGURED"


def test_short_calls_have_room_for_large_inputs():
    """60 秒的短档对带思考的大输入太紧；放宽后仍然只试两次。"""
    assert call_profile("hyp_novelty") == (120.0, 2)
    assert call_profile("hyp_ground") == (300.0, 2)
    assert call_profile("writing")[0] == 600.0


# ---- 替身 ----


class RiskNote(BaseModel):
    risk_note: str


class NoCannedReply(BaseModel):
    title: str = Field(min_length=6)
    kind: Literal["survey", "method"]
    tags: list[str] = Field(min_length=2)
    weight: int = Field(ge=3)
    nested: Verdict
    note: str | None


async def test_fake_validates_its_canned_reply_against_the_schema(app):
    result = await LLMRouter().complete_json(
        "hyp_feasibility",
        [Message("system", "POLARIS_HYP_FEASIBILITY\n..."), Message("user", "{}")],
        schema=RiskNote,
    )
    assert "fake 可行性" in result.data.risk_note and result.calls == 1
    assert result.usage["prompt_tokens"] > 0


async def test_fake_canned_reply_that_misses_the_schema_fails_loudly(app):
    class Wrong(BaseModel):
        risk: str

    with pytest.raises(errors.LLMSchemaError, match="canned reply does not match Wrong"):
        await LLMRouter().complete_json(
            "hyp_feasibility",
            [Message("system", "POLARIS_HYP_FEASIBILITY\n..."), Message("user", "{}")],
            schema=Wrong,
        )


async def test_fake_synthesizes_a_minimal_instance_without_a_responder(app):
    result = await LLMRouter().complete_json(
        "default", [Message("user", "anything")], schema=NoCannedReply
    )
    data = result.data
    assert data.kind == "survey" and len(data.title) >= 6
    assert len(data.tags) == 2 and data.weight == 3
    assert data.nested.label == "keep" and data.nested.score == 0.0
    assert data == minimal_instance(NoCannedReply)


async def test_fake_fail_next_injects_a_typed_error_once(app):
    router = LLMRouter()
    fake = FakeProvider()
    router.override_provider(fake)
    fake.fail_next(errors.LLMRateLimitedError("HTTP 429"), schema=Verdict)
    # 别的 schema 不受影响
    await router.complete_json("default", [Message("user", "x")], schema=RiskNote)
    with pytest.raises(errors.LLMRateLimitedError):
        await router.complete_json("default", [Message("user", "x")], schema=Verdict)
    assert (await router.complete_json("default", [Message("user", "x")], schema=Verdict)).data


async def test_providers_without_native_support_fall_back_to_a_schema_hint(app):
    """没有重写 complete_structured 的 provider（各种测试替身）：schema 写进提示，
    回答里夹着说明文字也能取出 JSON。"""

    class Plain(LLMProvider):
        def __init__(self) -> None:
            self.seen: list[list[Message]] = []

        async def complete(self, messages, *, model, **_kw):
            self.seen.append(list(messages))
            return CompletionResult(
                content='Here you go:\n{"risk_note": "thin evidence"}\nDone.', model=model
            )

        def stream(self, messages, *, model, **_kw):  # pragma: no cover
            raise NotImplementedError

    plain = Plain()
    router = LLMRouter()
    router.override_provider(plain)
    result = await router.complete_json(
        "default",
        [Message("system", "MARKER"), Message("user", "q")],
        schema=RiskNote,
        cache_prefix="shared corpus",
    )
    assert result.data.risk_note == "thin evidence"
    sent = plain.seen[0]
    assert [m.role for m in sent] == ["system", "system", "user"]
    assert sent[0].text == "MARKER" and '"risk_note"' in sent[1].text
    assert sent[2].text == "shared corpus\n\nq"
