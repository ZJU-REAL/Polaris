"""每日简报分批生成、遗漏补跑与最终综合重试。"""

import json
import re
import uuid

import pytest

from app.core.llm.base import CompletionResult
from app.models.library_direction import DirectionLibrary
from app.models.voyage import VoyageRun
from app.services.research_digest import (
    _generate_paper_insights,
    _synthesize_digest_summary,
)


class _RetryingDigestLLM:
    """首批漏一篇、首次综合返回坏 JSON，随后都成功。"""

    def __init__(self) -> None:
        self.batch_prompt_ids: list[list[str]] = []
        self.synthesis_calls = 0

    async def complete(self, stage, messages, **kwargs):
        assert stage == "librarian"
        system = messages[0].content
        user = messages[-1].content
        paper_ids = list(dict.fromkeys(re.findall(r'"paper_id"\s*:\s*"([^"]+)"', user)))
        if "POLARIS_DAILY_DIGEST_BATCH" in system:
            self.batch_prompt_ids.append(paper_ids)
            returned_ids = paper_ids[:-1] if len(self.batch_prompt_ids) == 1 else paper_ids
            return CompletionResult(
                content=json.dumps(
                    {
                        "paper_insights": [
                            {
                                "paper_id": paper_id,
                                "highlight": f"看点-{paper_id[-2:]}",
                                "direction_relation": "直接相关",
                                "concepts": ["Agent 工作流", "反馈闭环"],
                            }
                            for paper_id in returned_ids
                        ]
                    }
                ),
                model="fake-batch",
            )

        assert "POLARIS_DAILY_DIGEST_SYNTHESIS" in system
        self.synthesis_calls += 1
        if self.synthesis_calls == 1:
            return CompletionResult(content="bad json", model="fake-summary")
        return CompletionResult(
            content=json.dumps(
                {
                    "summary": "分批结果已综合。",
                    "cross_paper_signals": [
                        {
                            "title": "共同信号",
                            "summary": "多篇论文形成共同方向。",
                            "paper_ids": [paper_ids[0], str(uuid.uuid4())],
                        }
                    ],
                }
            ),
            model="fake-summary",
        )


@pytest.mark.asyncio
async def test_digest_batches_retry_only_missing_then_retry_synthesis():
    library_id = uuid.uuid4()
    library = DirectionLibrary(
        id=library_id,
        name="批处理测试库",
        definition={"statement": "agent harness"},
    )
    run = VoyageRun(
        id=uuid.uuid4(),
        kind="wiki_ingest",
        goal="生成今日简报",
        library_id=library_id,
        created_by=uuid.uuid4(),
    )
    papers = [
        {
            "paper_id": str(uuid.uuid4()),
            "title": f"Paper {index}",
            "tldr": f"TLDR {index}",
            "relevance_reason": "符合方向",
            "concepts": [],
        }
        for index in range(23)
    ]
    llm = _RetryingDigestLLM()

    insights, model, insight_retries, batch_count = await _generate_paper_insights(
        llm=llm,
        run=run,
        library=library,
        papers=papers,
        user_id=run.created_by,
        extra_guidance="",
    )

    assert len(insights) == 23
    assert [item["paper_id"] for item in insights] == [item["paper_id"] for item in papers]
    assert model == "fake-batch"
    assert batch_count == 3
    assert insight_retries == 1
    assert insights[0]["concepts"] == ["Agent 工作流", "反馈闭环"]
    # 10 篇首批漏 1 篇后，只补跑缺失的 1 篇；其余两批仍为 10 / 3。
    assert [len(ids) for ids in llm.batch_prompt_ids] == [10, 1, 10, 3]

    summary, signals, model, synthesis_retries = await _synthesize_digest_summary(
        llm=llm,
        run=run,
        library=library,
        paper_insights=insights,
        user_id=run.created_by,
        extra_guidance="",
    )
    assert summary == "分批结果已综合。"
    assert signals[0]["paper_ids"] == [papers[0]["paper_id"]]
    assert model == "fake-summary"
    assert synthesis_retries == 1
    assert llm.synthesis_calls == 2
