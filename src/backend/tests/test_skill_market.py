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




async def _login(client, email="alice@example.com"):
    token = await register_and_login(client, email=email)
    return {"Authorization": f"Bearer {token}"}


async def _publish(client, headers) -> tuple[str, str]:
    skill = (await client.post("/api/skills", json=SKILL_PAYLOAD, headers=headers)).json()
    resp = await client.post(
        f"/api/skills/{skill['id']}/publish",
        json={"summary": "推荐给大家", "tags": ["评分", "idea"]},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return skill["id"], resp.json()["id"]


async def test_publish_lists_immediately(client):
    headers = await _login(client)
    _skill_id, listing_id = await _publish(client, headers)

    # 发布即上架：任何登录用户立即可见
    headers_b = await _login(client, email="bob@example.com")
    market = (await client.get("/api/market/skills", headers=headers_b)).json()
    assert [m["id"] for m in market] == [listing_id]
    assert market[0]["status"] == "approved"
    assert market[0]["skill"]["slug"] == "market-rubric"
    assert market[0]["version"] == 1

    # 重复发布 409
    mine = (await client.get("/api/skills?scope=mine", headers=headers)).json()
    resp = await client.post(f"/api/skills/{mine[0]['id']}/publish", json={}, headers=headers)
    assert resp.status_code == 409

    # 详情含全文预览
    detail = (await client.get(f"/api/market/skills/{listing_id}", headers=headers)).json()
    assert detail["body"] == "严格打分。"


async def test_install_and_delist(client):
    headers_a = await _login(client)
    _skill_id, listing_id = await _publish(client, headers_a)

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
    market = (await client.get("/api/market/skills", headers=headers_b)).json()
    assert market[0]["install_count"] == 1

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
