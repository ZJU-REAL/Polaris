"""内联 AI 写作辅助测试：prompt 组装 / 越界引用检查 / SSE 端点（fake provider）。"""

import json

from app.models.manuscript import Manuscript
from app.services import writing_assist
from tests.test_manuscripts import _create_manuscript, _setup_project

FACT_PACK = {
    "citations": [
        {"bibkey": "smith2017attention", "title": "Attention Is All You Need", "year": 2017},
        {"bibkey": "lee2023retrieval", "title": "Retrieval Augmented Models", "year": 2023},
    ],
    "metrics": [{"name": "accuracy", "best": 0.8}],
    "figures": [{"fig_id": "exp_fig_0", "caption": "主指标曲线"}],
}


def _manuscript() -> Manuscript:
    return Manuscript(
        title="Co-citation Graph Retrieval", template="neurips2026", fact_pack=FACT_PACK
    )


def test_build_assist_messages_polish_includes_fact_pack():
    messages = writing_assist.build_assist_messages(
        _manuscript(), mode="polish", text="Our method is good.", before="prev", after="next"
    )
    assert messages[0].role == "system"
    assert "POLARIS_WRITING_ASSIST" in messages[0].content
    user = messages[1].content
    assert "smith2017attention" in user
    assert "accuracy = 0.8" in user
    assert "exp_fig_0" in user
    assert "Our method is good." in user
    assert "润色" in user


def test_build_assist_messages_modes():
    rewrite = writing_assist.build_assist_messages(
        _manuscript(), mode="rewrite", text="foo", instruction="更简洁"
    )
    assert "更简洁" in rewrite[1].content
    cont = writing_assist.build_assist_messages(
        _manuscript(), mode="continue", before="Intro text."
    )
    assert "续写" in cont[1].content
    assert "Intro text." in cont[1].content


def test_build_assist_messages_clamps_long_input():
    messages = writing_assist.build_assist_messages(
        _manuscript(), mode="polish", text="x" * 50_000, before="y" * 20_000
    )
    assert len(messages[1].content) < 30_000


def test_scan_result_warnings():
    ok = writing_assist.scan_result_warnings(
        FACT_PACK,
        "See \\cite{smith2017attention} and \\includegraphics[width=1cm]{figures/exp_fig_0.pdf}.",
    )
    assert ok == []
    bad = writing_assist.scan_result_warnings(
        FACT_PACK, "As \\citep{made2024up} shows \\includegraphics{figures/ghost.pdf}."
    )
    assert len(bad) == 2
    assert "made2024up" in bad[0]
    assert "ghost" in bad[1]


def _parse_sse(text: str) -> list[tuple[str, dict]]:
    events = []
    for block in text.strip().split("\n\n"):
        event, data = None, None
        for line in block.split("\n"):
            if line.startswith("event: "):
                event = line[len("event: ") :]
            elif line.startswith("data: "):
                data = json.loads(line[len("data: ") :])
        if event is not None:
            events.append((event, data))
    return events


async def test_assist_endpoint_streams_delta_and_done(client):
    project_id, headers = await _setup_project(client)
    resp = await _create_manuscript(client, headers, project_id)
    ms_id = resp.json()["id"]

    async with client.stream(
        "POST",
        f"/api/manuscripts/{ms_id}/assist",
        json={"mode": "polish", "text": "Our approach improve the results."},
        headers=headers,
    ) as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        body = ""
        async for chunk in resp.aiter_text():
            body += chunk
    events = _parse_sse(body)
    kinds = [e for e, _ in events]
    assert "delta" in kinds
    assert kinds[-1] == "done"
    done = events[-1][1]
    assert done["usage"]["completion_tokens"] > 0


async def test_assist_endpoint_validation(client):
    project_id, headers = await _setup_project(client)
    resp = await _create_manuscript(client, headers, project_id)
    ms_id = resp.json()["id"]

    resp = await client.post(
        f"/api/manuscripts/{ms_id}/assist", json={"mode": "polish", "text": "  "}, headers=headers
    )
    assert resp.status_code == 422
    assert resp.json()["detail"] == "ASSIST_TEXT_REQUIRED"

    resp = await client.post(
        f"/api/manuscripts/{ms_id}/assist",
        json={"mode": "rewrite", "text": "foo"},
        headers=headers,
    )
    assert resp.status_code == 422
    assert resp.json()["detail"] == "ASSIST_INSTRUCTION_REQUIRED"

    resp = await client.post(
        f"/api/manuscripts/{ms_id}/assist", json={"mode": "continue"}, headers=headers
    )
    assert resp.status_code == 422
    assert resp.json()["detail"] == "ASSIST_BEFORE_REQUIRED"


async def test_assist_endpoint_requires_membership(client):
    project_id, headers = await _setup_project(client)
    resp = await _create_manuscript(client, headers, project_id)
    ms_id = resp.json()["id"]

    _, other_headers = await _setup_project(client, email="mallory@example.com")
    resp = await client.post(
        f"/api/manuscripts/{ms_id}/assist",
        json={"mode": "polish", "text": "hello"},
        headers=other_headers,
    )
    assert resp.status_code == 404
