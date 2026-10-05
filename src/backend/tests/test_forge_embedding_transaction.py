"""Forge embedding calls must not hold a writer while separate usage sessions write."""

import uuid

from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError

from app.agents.voyage.actions import ActionContext
from app.agents.voyage.actions_ideas import forge_dedup
from app.core.db import get_sessionmaker
from app.core.llm.router import get_llm_router
from app.models.idea import Idea
from app.models.llm_config import LLMUsage
from app.models.system_setting import SystemSetting
from app.models.voyage import VoyageRun
from tests.test_idea_forge import (
    FAKE_IDEA_1_SUMMARY,
    FAKE_IDEA_1_TITLE,
    KNOBS,
    _setup_project,
)


async def test_second_dedup_embedding_allows_independent_writer_and_records_both_usages(
    client, monkeypatch, caplog,
):
    project_id, _ = await _setup_project(client)
    async with get_sessionmaker()() as session:
        existing = Idea(project_id=uuid.UUID(project_id), title=FAKE_IDEA_1_TITLE,
                        summary=FAKE_IDEA_1_SUMMARY, status="candidate")
        run = VoyageRun(kind="idea_forge", goal="test two-batch dedup", status="executing",
                        project_id=uuid.UUID(project_id), cursor=0)
        session.add_all([existing, run])
        await session.commit()
        run_id = run.id
        assert await session.get(SystemSetting, "embedding_active_space") is None
    router = get_llm_router()
    original_embed = router.embed
    calls = 0
    writer_errors = []

    async def checked_embed(texts, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            # This is the actual second LLM boundary, after existing-Idea queries
            # have autoflushed a newly initialized active embedding space.
            async with get_sessionmaker()() as writer:
                await writer.execute(text("PRAGMA busy_timeout=50"))
                writer.add(SystemSetting(key="dedup_writer_probe", value={"written": True}))
                try:
                    await writer.commit()
                except OperationalError as exc:
                    writer_errors.append(str(exc))
                    await writer.rollback()
        return await original_embed(texts, **kwargs)

    monkeypatch.setattr(router, "embed", checked_embed)
    ctx = ActionContext(run=run, llm=router, checkpoint={
        "params": {"knobs": KNOBS}, "forge_candidates": [{
            "title": FAKE_IDEA_1_TITLE, "summary": FAKE_IDEA_1_SUMMARY,
        }],
    })
    observation = await forge_dedup(ctx, {})
    assert calls == 2
    assert writer_errors == []
    assert observation["existing_compared"] == 1 and observation["dropped"] == 1
    assert not any("llm usage accounting failed" in record.message for record in caplog.records)
    async with get_sessionmaker()() as session:
        marker = await session.get(SystemSetting, "dedup_writer_probe")
        assert marker is not None and marker.value == {"written": True}
        usage = (await session.scalars(select(LLMUsage).where(
            LLMUsage.voyage_id == run_id, LLMUsage.stage == "embedding",
        ))).all()
        assert len(usage) == 2
        assert all(row.prompt_tokens > 0 for row in usage)
