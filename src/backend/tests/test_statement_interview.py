"""方向描述访谈：结构化问答产出 statement。

起因是生产上 12 个文献库的 statement 全是 3~31 字符的标签，其中 CUA 那个只有三个字母。
拿缩写去嵌入，全池排第一的是超图小样本学习论文，而真正的 GUI agent 论文排在几百名开外；
换成一段正常描述后那些无关论文掉到 200~600 名。写作提示挡不住这个，得改成引导式产出。
"""

import uuid

from app.services import statement_interview as interview
from tests.conftest import register_and_login


async def _headers(client, email="interview@example.com"):
    token = await register_and_login(client, email=email)
    return {"Authorization": f"Bearer {token}"}


async def test_interview_walks_four_stages_then_writes_a_statement(client):
    """四个环节逐题问，答满之后直接给出写好的描述。"""
    headers = await _headers(client)
    answers: list[dict] = []
    seen: list[str] = []

    for expected_step in (1, 2, 3, 4):
        resp = await client.post(
            "/api/libraries/statement-interview",
            json={"topic": "Computer-use agents", "answers": answers},
            headers=headers,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["done"] is False
        assert body["step"] == expected_step and body["total"] == 4
        q = body["question"]
        assert q["question"].strip(), "问题不能是空的"
        seen.append(q["stage"])
        answers.append({"stage": q["stage"], "selected": q["options"][:1], "custom": ""})

    assert seen == list(interview.STAGE_KEYS), "环节顺序应当是固定的"

    resp = await client.post(
        "/api/libraries/statement-interview",
        json={"topic": "Computer-use agents", "answers": answers},
        headers=headers,
    )
    body = resp.json()
    assert body["done"] is True
    assert body["question"] is None
    assert isinstance(body["statement"], str)


async def test_interview_is_stateless_and_resumes_from_answers(client):
    """只带前两个环节的回答，服务端就该问第三个——不依赖任何会话状态。"""
    headers = await _headers(client, email="resume@example.com")
    partial = [
        {"stage": "question", "selected": ["a"], "custom": ""},
        {"stage": "subject", "selected": [], "custom": "GUI 智能体"},
    ]
    resp = await client.post(
        "/api/libraries/statement-interview",
        json={"topic": "CUA", "answers": partial},
        headers=headers,
    )
    body = resp.json()
    assert body["done"] is False
    assert body["question"]["stage"] == "subproblems"
    assert body["step"] == 3


async def test_interview_survives_an_unavailable_model(client, monkeypatch):
    """模型挂了也要能往下走：退回开放题让用户自己写，不能把访谈卡死。"""
    headers = await _headers(client, email="llmdown@example.com")

    async def boom(*args, **kwargs):
        raise RuntimeError("model unavailable")

    from app.core.llm.router import LLMRouter

    monkeypatch.setattr(LLMRouter, "complete", boom)

    resp = await client.post(
        "/api/libraries/statement-interview",
        json={"topic": "CUA", "answers": []},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["done"] is False
    assert body["question"]["options"] == [], "拿不到候选时应当只留自由填写"
    assert body["question"]["question"].strip()


def test_answer_text_joins_selection_and_custom():
    a = interview.Answer(stage="methods", selected=["方法 A", "方法 B"], custom="另外还要 C")
    assert a.as_text() == "方法 A；方法 B；另外还要 C"
    assert interview.Answer(stage="methods", selected=[], custom="").as_text() == "（跳过）"


def test_next_stage_follows_the_fixed_order():
    assert interview.next_stage([]) == "question"
    answered = [interview.Answer(stage=k, selected=[]) for k in interview.STAGE_KEYS[:3]]
    assert interview.next_stage(answered) == "methods"
    all_done = [interview.Answer(stage=k, selected=[]) for k in interview.STAGE_KEYS]
    assert interview.next_stage(all_done) is None


async def test_creating_a_library_now_requires_a_real_statement(client):
    """statement 变必填。

    只挡空值，不挡短值——长度不是质量的代理，「CUA」这类标签靠界面上的访谈来避免，
    而不是靠一个武断的字数门槛（它会误伤正当的短描述）。
    """
    headers = await _headers(client, email="required@example.com")

    resp = await client.post("/api/libraries", json={"name": "CUA"}, headers=headers)
    assert resp.status_code == 422, "缺 statement 应当被拒"

    good = (
        "Computer-use agents that operate graphical interfaces: GUI agents on desktop and "
        "mobile, browser-use agents, screen understanding and action grounding, and the "
        "benchmarks used to evaluate them."
    )
    resp = await client.post(
        "/api/libraries", json={"name": "CUA", "statement": good}, headers=headers
    )
    assert resp.status_code in (200, 201), resp.text
    assert uuid.UUID(resp.json()["id"])
