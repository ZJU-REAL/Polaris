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
        assert stage == "digest"
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

    (
        insights,
        model,
        insight_retries,
        batch_count,
        failures,
    ) = await _generate_paper_insights(
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
    assert failures == [], "补跑成功就不该记失败"

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


class _AlwaysMissingDigestLLM:
    """指定的几篇论文永远不返回，其余都正常——模拟模型对某几篇死活不出结果。"""

    def __init__(self, doomed_ids: set[str]) -> None:
        self.doomed_ids = doomed_ids
        self.calls = 0

    async def complete(self, stage, messages, **kwargs):
        assert stage == "digest"
        user = messages[-1].content
        paper_ids = list(dict.fromkeys(re.findall(r'"paper_id"\s*:\s*"([^"]+)"', user)))
        self.calls += 1
        return CompletionResult(
            content=json.dumps(
                {
                    "paper_insights": [
                        {
                            "paper_id": paper_id,
                            "highlight": f"看点-{paper_id[-2:]}",
                            "direction_relation": "直接相关",
                            # 概念是简报必填项，空数组会被当成漏项——这里给足，只用
                            # doomed_ids 控制"哪几篇模型死活不返回"。
                            "concepts": ["Agent 工作流"],
                        }
                        for paper_id in paper_ids
                        if paper_id not in self.doomed_ids
                    ]
                }
            ),
            model="fake-batch",
        )


@pytest.mark.asyncio
async def test_a_batch_that_never_completes_degrades_instead_of_raising():
    """一批三轮补跑仍缺，就交回已拿到的部分并记原因——不能把整轮同步打成 paused_error。

    简报是附加产物：以前这里抛 RuntimeError，模型偶尔漏几篇就会让整次库同步失败，
    连已经打分入库的论文都看不到简报。
    """
    library_id = uuid.uuid4()
    library = DirectionLibrary(
        id=library_id, name="降级测试库", definition={"statement": "agent harness"}
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
        for index in range(20)
    ]

    # 只让第二批里的一篇死活不出结果
    llm = _AlwaysMissingDigestLLM(doomed_ids={papers[12]["paper_id"]})
    insights, model, retries, batch_count, failures = await _generate_paper_insights(
        llm=llm,
        run=run,
        library=library,
        papers=papers,
        user_id=run.created_by,
        extra_guidance="",
    )

    assert batch_count == 2
    assert model == "fake-batch"
    assert len(failures) == 1, f"坏批次应当恰好记一条原因：{failures}"
    assert "batch 2/2" in failures[0] and papers[12]["paper_id"] in failures[0]
    # 20 篇一篇不少，其余 19 篇拿到模型的看点，缺的那篇退回自己的 TL;DR
    assert [item["paper_id"] for item in insights] == [item["paper_id"] for item in papers]
    stuck = next(i for i in insights if i["paper_id"] == papers[12]["paper_id"])
    assert stuck["highlight"] == papers[12]["tldr"]
    assert stuck["direction_relation"] == "符合方向"
    assert all(
        item["highlight"].startswith("看点-")
        for item in insights
        if item["paper_id"] != papers[12]["paper_id"]
    )
    assert retries == 2, "该批应当补跑到上限（共三轮）"
