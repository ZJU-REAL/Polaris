"""对话 SSE 骨架收敛后的共同契约。

这段骨架此前是三份逐字相同的副本（wiki / papers / libraries），daily 还从 wiki 反向
import 了一份。三份各自演化的结果是**行为已经不一致**：只有 papers 那份带了
``X-Accel-Buffering: no``，另外两个在 nginx 后面会被缓冲，流式变成「转很久然后一次性
蹦出全文」。收敛之后这里钉住四个入口的响应头一致。
"""

import uuid

from app.core.db import get_sessionmaker
from tests.conftest import add_paper, register_and_login


async def _project(client, headers, name="sse-headers"):
    resp = await client.post(
        "/api/projects", json={"name": name, "statement": "LLM agent"}, headers=headers
    )
    return uuid.UUID(resp.json()["id"])


async def test_every_chat_endpoint_streams_with_the_same_headers(client):
    """四个对话入口的流式响应头必须一字不差。

    少一个 X-Accel-Buffering 就意味着那个入口在反向代理后面不是流式的——用户看到的
    是「一直转圈，然后整段蹦出来」，而其余入口正常。这种不一致极难从现象反推。
    """
    token = await register_and_login(client, email="sse-headers@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    project_id = await _project(client, headers)

    async with get_sessionmaker()() as session:
        paper = await add_paper(
            session,
            project_id=project_id,
            title="Planning with Tree Search",
            abstract="planning abstract",
            tldr="一句话总结",
            year=2025,
            status="included",
        )
        await session.commit()
        paper_id = paper.id

    body = {"question": "这个方向有哪些方法？", "history": []}
    endpoints = [
        f"/api/projects/{project_id}/chat",
        f"/api/projects/{project_id}/shelf/chat",
        "/api/library/chat",
        f"/api/papers/{paper_id}/chat",
    ]

    seen: dict[str, dict[str, str]] = {}
    for path in endpoints:
        async with client.stream("POST", path, json=body, headers=headers) as resp:
            assert resp.status_code == 200, f"{path}: {resp.status_code}"
            assert resp.headers["content-type"].startswith("text/event-stream"), path
            seen[path] = {
                k: resp.headers.get(k, "(缺失)")
                for k in ("cache-control", "connection", "x-accel-buffering")
            }
            await resp.aread()

    for path, got in seen.items():
        assert got["x-accel-buffering"] == "no", f"{path} 会被 nginx 缓冲：{got}"
        assert got["cache-control"] == "no-cache", f"{path}: {got}"

    # 四个入口给出的是同一组头，不是各写各的
    assert len(set(map(str, seen.values()))) == 1, seen


async def test_sse_frame_serialises_uuid_and_datetime(client):
    """帧序列化必须带 default=str。

    payload 里常有 UUID / datetime；没有 default=str 会抛 TypeError 把整条流打断，
    而不是序列化成字符串——收敛这段代码时我第一版就漏了它。
    """
    import datetime as dt
    import json

    from app.api.chat_stream import sse_frame

    frame = sse_frame("x", {"id": uuid.uuid4(), "at": dt.datetime.now(dt.UTC)})
    assert frame.startswith("event: x\ndata: ")
    assert frame.endswith("\n\n")
    payload = json.loads(frame.split("data: ", 1)[1])
    assert isinstance(payload["id"], str) and isinstance(payload["at"], str)


async def test_paper_chat_omits_sources_when_there_are_none(client):
    """AI 伴读没有参考文献时不发 sources 帧——它的语料就是当前这一篇。

    其余入口恒发一帧（前端据此渲染来源清单，收不到就一直是加载态）。收敛时这个差异
    是唯一需要参数化的地方，别把它抹平了。
    """
    import json

    token = await register_and_login(client, email="sse-paper@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    project_id = await _project(client, headers, name="sse-paper")
    async with get_sessionmaker()() as session:
        paper = await add_paper(
            session,
            project_id=project_id,
            title="Solo",
            abstract="a",
            tldr="t",
            year=2025,
            status="included",
        )
        await session.commit()
        paper_id = paper.id

    async with client.stream(
        "POST",
        f"/api/papers/{paper_id}/chat",
        json={"question": "讲讲这篇", "history": []},
        headers=headers,
    ) as resp:
        text = (await resp.aread()).decode("utf-8")

    kinds = [
        line[len("event: ") :]
        for line in text.splitlines()
        if line.startswith("event: ")
    ]
    assert "sources" not in kinds, f"没有参考文献时不该发 sources：{kinds}"
    assert kinds and kinds[-1] == "done"
    assert json.loads(text.rsplit("data: ", 1)[1])["usage"]["completion_tokens"] > 0
