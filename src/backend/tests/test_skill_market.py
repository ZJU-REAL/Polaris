"""技能市场（S4）与 output_contract 校验测试。"""

import uuid

from app.agents.voyage.sextant import Sextant
from app.agents.voyage.skillset import check_output_contract
from app.core.llm.router import LLMRouter
from app.models.voyage import VoyageRun
from tests.conftest import register_and_login

SKILL_PAYLOAD = {
    "slug": "market-rubric",
    "kind": "rubric",
    "name": "可发布的打分标准",
    "description": "market 测试用",
    "manifest": {"targets": ["forge.score"]},
    "body": "严格打分。",
}


# 注意：首个注册用户自动成为平台 admin（auth 自举逻辑），
# 因此 alice = 管理员兼发布者，403 断言用第二个用户 bob（member）


async def _login(client, email="alice@example.com"):
    token = await register_and_login(client, email=email)
    return {"Authorization": f"Bearer {token}"}


async def _publish(client, headers) -> tuple[str, str]:
    skill = (await client.post("/api/skills", json=SKILL_PAYLOAD, headers=headers)).json()
    resp = await client.post(
        f"/api/skills/{skill['id']}/publish",
        json={"summary": "推荐给全实验室", "tags": ["评分", "idea"]},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return skill["id"], resp.json()["id"]


async def test_publish_review_flow(client):
    headers = await _login(client)  # alice：首个用户 = admin
    _skill_id, listing_id = await _publish(client, headers)

    # 重复发布 409
    mine = (await client.get("/api/skills?scope=mine", headers=headers)).json()
    resp = await client.post(f"/api/skills/{mine[0]['id']}/publish", json={}, headers=headers)
    assert resp.status_code == 409

    # 非管理员（bob）看不了审核队列、不能审核
    headers_b = await _login(client, email="bob@example.com")
    assert (
        await client.get("/api/market/skills?status=pending", headers=headers_b)
    ).status_code == 403
    assert (
        await client.post(f"/api/market/skills/{listing_id}/approve", headers=headers_b)
    ).status_code == 403
    # 管理员能看到审核队列
    pending = (await client.get("/api/market/skills?status=pending", headers=headers)).json()
    assert [p["id"] for p in pending] == [listing_id]

    # pending 条目不出现在市场列表
    assert (await client.get("/api/market/skills", headers=headers)).json() == []

    # 管理员审核通过
    resp = await client.post(
        f"/api/market/skills/{listing_id}/approve", json={"comment": "内容合规"}, headers=headers
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "approved"
    # 二次审核 409
    assert (
        await client.post(f"/api/market/skills/{listing_id}/approve", headers=headers)
    ).status_code == 409

    market = (await client.get("/api/market/skills", headers=headers)).json()
    assert [m["id"] for m in market] == [listing_id]
    assert market[0]["skill"]["slug"] == "market-rubric"
    assert market[0]["version"] == 1

    # 详情含全文预览
    detail = (await client.get(f"/api/market/skills/{listing_id}", headers=headers)).json()
    assert detail["body"] == "严格打分。"


async def test_install_rating_and_delist(client):
    headers_a = await _login(client)  # alice：首个用户 = admin
    _skill_id, listing_id = await _publish(client, headers_a)
    await client.post(f"/api/market/skills/{listing_id}/approve", headers=headers_a)

    # 另一个用户安装 → 拷为自己的 user 技能
    headers_b = await _login(client, email="bob@example.com")
    resp = await client.post(f"/api/market/skills/{listing_id}/install", headers=headers_b)
    assert resp.status_code == 201, resp.text
    installed = resp.json()
    assert installed["scope"] == "user"
    assert installed["slug"] == "market-rubric"
    assert installed["current_version"]["body"] == "严格打分。"
    mine_b = (await client.get("/api/skills?scope=mine", headers=headers_b)).json()
    assert [s["slug"] for s in mine_b] == ["market-rubric"]

    # 评分（可更新，同人只一条）+ 聚合
    await client.post(
        f"/api/market/skills/{listing_id}/reviews",
        json={"rating": 4, "comment": "好用"},
        headers=headers_b,
    )
    resp = await client.post(
        f"/api/market/skills/{listing_id}/reviews", json={"rating": 5}, headers=headers_b
    )
    assert resp.status_code == 201
    reviews = (
        await client.get(f"/api/market/skills/{listing_id}/reviews", headers=headers_b)
    ).json()
    assert len(reviews) == 1 and reviews[0]["rating"] == 5
    market = (await client.get("/api/market/skills", headers=headers_b)).json()
    assert market[0]["install_count"] == 1
    assert market[0]["rating_avg"] == 5.0 and market[0]["rating_count"] == 1

    # 发布者下架 → 市场不可见、安装 409
    resp = await client.delete(f"/api/market/skills/{listing_id}", headers=headers_a)
    assert resp.json()["status"] == "delisted"
    assert (await client.get("/api/market/skills", headers=headers_b)).json() == []
    assert (
        await client.post(f"/api/market/skills/{listing_id}/install", headers=headers_b)
    ).status_code == 409


# ---- output_contract → Sextant 确定性校验 ----

CONTRACT = {
    "format": "json",
    "json_schema": {
        "type": "object",
        "required": ["score", "reason"],
        "properties": {"score": {"type": "number"}, "reason": {"type": "string"}},
    },
}


def test_check_output_contract():
    ok = '```json\n{"score": 0.8, "reason": "好"}\n```'
    assert check_output_contract(CONTRACT, ok) is None
    assert "不是合法 JSON" in check_output_contract(CONTRACT, "随便说点什么")
    assert "缺少必填字段" in check_output_contract(CONTRACT, '{"score": 0.8}')
    assert "应为 number" in check_output_contract(CONTRACT, '{"score": "高", "reason": "x"}')
    # 非 json 格式约定不做确定性校验
    assert check_output_contract({"format": "markdown"}, "任意文本") is None


async def test_sextant_contract_gate():
    run = VoyageRun(
        kind="custom",
        goal="g",
        status="verifying",
        cursor=0,
        checkpoint={
            "skills": {
                "llm.complete": [
                    {
                        "slug": "c",
                        "name": "约定",
                        "kind": "rubric",
                        "version": 1,
                        "body": "x",
                        "output_contract": CONTRACT,
                    }
                ]
            }
        },
        project_id=uuid.uuid4(),
        created_by=None,
    )
    sextant = Sextant(LLMRouter())
    step = {"action": "llm.complete", "title": "打分", "acceptance": "输出打分 JSON"}

    verdict, usage = await sextant.verify(run, step, {"content": "这不是 JSON"})
    assert verdict["passed"] is False
    assert "不符合技能约定" in verdict["reason"]
    assert usage == {}  # 未调用 LLM

    # 合法 JSON → 过确定性校验，继续走 LLM 判定（fake provider）
    verdict, _usage = await sextant.verify(run, step, {"content": '{"score": 1, "reason": "好"}'})
    assert "passed" in verdict
