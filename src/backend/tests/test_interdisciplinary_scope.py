"""Structured scope generation remains useful when model output is malformed."""

import uuid

import pytest

from app.core.llm.base import CompletionResult
from app.schemas.interdisciplinary import InterdisciplinaryScopeSuggestRequest
from app.services.interdisciplinary_scope import parse_suggestion, suggest_scope


def test_parse_suggestion_accepts_wrapped_aliases_and_trailing_commas():
    result = parse_suggestion(
        """```json
        {"data": {
          "交叉研究范围": "Study impact response using visual measurements.",
          "核心交叉问题": ["How do image features map to mechanical response?"],
          "主学科": "Structural engineering",
          "关联学科": "Computer vision, Data science",
          "理由": "The method and validation evidence come from different domains",
        }}
        ```""",
        model="test-model",
    )

    assert result.primary_domain == "Structural engineering"
    assert result.related_domains == ["Computer vision", "Data science"]
    assert result.model == "test-model"


@pytest.mark.asyncio
async def test_invalid_model_output_uses_concrete_evidence_fallback():
    class InvalidRouter:
        def __init__(self):
            self.calls = 0

        async def complete(self, *args, **kwargs):
            self.calls += 1
            return CompletionResult(content="not valid json", model="broken-model")

    router = InvalidRouter()
    result = await suggest_scope(
        InterdisciplinaryScopeSuggestRequest(
            name="SAM3-assisted impact response",
            statement="Use SAM3 segmentation to study structural failure under impact load.",
        ),
        user_id=uuid.uuid4(),
        llm=router,
    )

    assert router.calls == 2
    assert result.primary_domain == "Structural engineering"
    assert "Computer vision" in result.related_domains
    assert "Pending" not in result.model
    assert result.model == "broken-model:evidence-fallback"
