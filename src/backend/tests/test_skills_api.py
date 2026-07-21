"""技能系统测试（docs/skill-system.md S1）：
CRUD/版本/fork/归档 · 启用到项目 · 内置种子幂等 · Voyage 快照与 guidance 注入。"""

import uuid

from app.agents.voyage.engine import VoyageEngine
from app.agents.voyage.skillset import skill_guidance, skill_personas
from app.core.db import get_sessionmaker
from app.core.llm.router import LLMRouter
from app.models.voyage import VoyageRun
from app.services.builtin_skills import BUILTIN_SKILLS
from app.services.skills import ensure_builtin_skills, snapshot_for_project
from tests.conftest import RecordingBus, register_and_login

SKILL_BODY = "打分时优先考虑可复现性：没有开源代码的方法降一档。"


async def _setup(client, email="alice@example.com"):
    token = await register_and_login(client, email=email)
    headers = {"Authorization": f"Bearer {token}"}
    resp = await client.post("/api/projects", json={"name": "skill-proj"}, headers=headers)
    return headers, resp.json()["id"]


def _skill_payload(slug="my-scoring", targets=None):
    return {
        "slug": slug,
        "kind": "rubric",
        "name": "我的打分标准",
        "description": "自定义 idea 打分细则",
        "manifest": {"targets": targets or ["forge.score"]},
        "body": SKILL_BODY,
    }


# ---- 技能 CRUD ----


async def test_create_get_and_list_skill(client):
    headers, _ = await _setup(client)
    resp = await client.post("/api/skills", json=_skill_payload(), headers=headers)
    assert resp.status_code == 201, resp.text
    skill = resp.json()
    assert skill["scope"] == "user"
    assert skill["current_version"]["version"] == 1
    assert skill["current_version"]["body"] == SKILL_BODY

    resp = await client.get(f"/api/skills/{skill['id']}", headers=headers)
    assert resp.status_code == 200
    resp = await client.get("/api/skills?scope=mine", headers=headers)
    assert [s["slug"] for s in resp.json()] == ["my-scoring"]

    # 他人不可见
    token_b = await register_and_login(client, email="bob@example.com")
    resp = await client.get(
        f"/api/skills/{skill['id']}", headers={"Authorization": f"Bearer {token_b}"}
    )
    assert resp.status_code == 404


async def test_create_skill_validation(client):
    headers, _ = await _setup(client)
    bad_slug = _skill_payload(slug="Bad Slug!")
    assert (await client.post("/api/skills", json=bad_slug, headers=headers)).status_code == 422

    bad_target = _skill_payload(targets=["not.a.target"])
    assert (await client.post("/api/skills", json=bad_target, headers=headers)).status_code == 422

    dup = _skill_payload()
    assert (await client.post("/api/skills", json=dup, headers=headers)).status_code == 201
    assert (await client.post("/api/skills", json=dup, headers=headers)).status_code == 409

    # persona 技能必须带人设；workflow 技能 steps 必须是白名单动作
    persona = _skill_payload(slug="my-personas") | {"kind": "persona"}
    assert (await client.post("/api/skills", json=persona, headers=headers)).status_code == 422
    workflow = _skill_payload(slug="my-flow") | {
        "kind": "workflow",
        "manifest": {
            "targets": ["navigator.free_plan"],
            "steps": [{"title": "x", "action": "no.such.action", "params": {}}],
        },
    }
    assert (await client.post("/api/skills", json=workflow, headers=headers)).status_code == 422


async def test_add_version_and_immutability(client):
    headers, _ = await _setup(client)
    skill = (await client.post("/api/skills", json=_skill_payload(), headers=headers)).json()
    resp = await client.post(
        f"/api/skills/{skill['id']}/versions",
        json={
            "manifest": {"targets": ["forge.score"]},
            "body": SKILL_BODY + "\n新增：与知识库重叠的想法新颖性上限 5。",
            "changelog": "补充新颖性约束",
        },
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["version"] == 2

    versions = (await client.get(f"/api/skills/{skill['id']}/versions", headers=headers)).json()
    assert [v["version"] for v in versions] == [2, 1]
    detail = (await client.get(f"/api/skills/{skill['id']}", headers=headers)).json()
    assert detail["current_version"]["version"] == 2


async def test_builtin_seed_readonly_and_fork(client):
    headers, _ = await _setup(client)
    async with get_sessionmaker()() as session:
        created = await ensure_builtin_skills(session)
        assert created == len(BUILTIN_SKILLS)
        assert await ensure_builtin_skills(session) == 0  # 幂等

    builtin = (await client.get("/api/skills?scope=builtin", headers=headers)).json()
    assert len(builtin) == len(BUILTIN_SKILLS)
    rubric = next(s for s in builtin if s["slug"] == "idea-scoring-rubric")

    # 内置只读：追加版本 / 归档均 403
    resp = await client.post(
        f"/api/skills/{rubric['id']}/versions",
        json={"manifest": {"targets": ["forge.score"]}, "body": "x"},
        headers=headers,
    )
    assert resp.status_code == 403
    assert (await client.delete(f"/api/skills/{rubric['id']}", headers=headers)).status_code == 403

    # fork 为我的技能：slug 冲突自动加后缀
    fork = (await client.post(f"/api/skills/{rubric['id']}/fork", headers=headers)).json()
    assert fork["scope"] == "user"
    assert fork["slug"] == "idea-scoring-rubric-2"
    assert fork["current_version"]["version"] == 1
    assert fork["current_version"]["body"]  # 内容拷贝自内置当前版


# ---- 启用到项目 ----


async def test_enable_skill_to_project(client):
    headers, project_id = await _setup(client)
    skill = (await client.post("/api/skills", json=_skill_payload(), headers=headers)).json()

    resp = await client.post(
        f"/api/projects/{project_id}/skills",
        json={"skill_id": skill["id"], "target": "forge.score", "config": {"strict": True}},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    enable = resp.json()
    assert enable["target"] == "forge.score"
    assert enable["skill"]["slug"] == "my-scoring"

    # 重复启用同 target → 409；技能未声明的 target → 422
    resp = await client.post(
        f"/api/projects/{project_id}/skills",
        json={"skill_id": skill["id"], "target": "forge.score"},
        headers=headers,
    )
    assert resp.status_code == 409
    resp = await client.post(
        f"/api/projects/{project_id}/skills",
        json={"skill_id": skill["id"], "target": "writing.section"},
        headers=headers,
    )
    assert resp.status_code == 422

    # 非成员 404
    token_b = await register_and_login(client, email="bob@example.com")
    resp = await client.get(
        f"/api/projects/{project_id}/skills", headers={"Authorization": f"Bearer {token_b}"}
    )
    assert resp.status_code == 404

    # PATCH 停用 + DELETE
    resp = await client.patch(
        f"/api/project-skills/{enable['id']}", json={"enabled": False}, headers=headers
    )
    assert resp.json()["enabled"] is False
    resp = await client.delete(f"/api/project-skills/{enable['id']}", headers=headers)
    assert resp.status_code == 204
    rows = (await client.get(f"/api/projects/{project_id}/skills", headers=headers)).json()
    assert rows == []


async def test_snapshot_pin_and_latest(client):
    headers, project_id = await _setup(client)
    skill = (await client.post("/api/skills", json=_skill_payload(), headers=headers)).json()
    v1_id = skill["current_version"]["id"]
    await client.post(
        f"/api/projects/{project_id}/skills",
        json={"skill_id": skill["id"], "target": "forge.score", "version_id": v1_id},
        headers=headers,
    )
    # 追加 v2 后：pin 住的启用仍取 v1
    await client.post(
        f"/api/skills/{skill['id']}/versions",
        json={"manifest": {"targets": ["forge.score"]}, "body": "v2 body"},
        headers=headers,
    )
    async with get_sessionmaker()() as session:
        snapshot = await snapshot_for_project(session, uuid.UUID(project_id))
    assert [e["version"] for e in snapshot["forge.score"]] == [1]

    # 解除 pin → 跟随最新
    enable_id = (await client.get(f"/api/projects/{project_id}/skills", headers=headers)).json()[0][
        "id"
    ]
    await client.patch(
        f"/api/project-skills/{enable_id}", json={"unpin_version": True}, headers=headers
    )
    async with get_sessionmaker()() as session:
        snapshot = await snapshot_for_project(session, uuid.UUID(project_id))
    assert [e["version"] for e in snapshot["forge.score"]] == [2]
    assert snapshot["forge.score"][0]["body"] == "v2 body"


# ---- SkillSet 渲染与 Voyage 快照 ----


def _checkpoint(kind="rubric", target="forge.score", body=SKILL_BODY):
    return {
        "skills": {
            target: [
                {
                    "slug": "my-scoring",
                    "name": "我的打分标准",
                    "kind": kind,
                    "version": 3,
                    "body": body,
                    "config": {"strict": True},
                    "personas": [{"name": "复现怀疑派", "stance": "专挑不可复现"}]
                    if kind == "persona"
                    else [],
                }
            ]
        }
    }


def test_skill_guidance_rendering():
    text = skill_guidance(_checkpoint(), "forge.score")
    assert "【项目技能指引】" in text
    assert "我的打分标准（my-scoring v3）" in text
    assert SKILL_BODY in text
    assert "strict=True" in text
    # 空场景返回空串；persona 技能不进 guidance 文本
    assert skill_guidance(None, "forge.score") == ""
    assert skill_guidance(_checkpoint(), "writing.section") == ""
    assert skill_guidance(_checkpoint(kind="persona"), "forge.score") == ""


def test_skill_guidance_truncation_and_personas():
    text = skill_guidance(_checkpoint(body="长" * 30000), "forge.score")
    assert "已截断" in text
    personas = skill_personas(_checkpoint(kind="persona"), "forge.score")
    assert personas == [{"name": "复现怀疑派", "stance": "专挑不可复现"}]
    assert skill_personas(_checkpoint(), "forge.score") is None


async def test_voyage_snapshots_enabled_skills(client, queue_stub, bus_recorder):
    headers, project_id = await _setup(client)
    skill = (await client.post("/api/skills", json=_skill_payload(), headers=headers)).json()
    await client.post(
        f"/api/projects/{project_id}/skills",
        json={"skill_id": skill["id"], "target": "forge.score"},
        headers=headers,
    )
    resp = await client.post(
        "/api/voyages",
        json={"kind": "demo", "project_id": project_id, "goal": "测试技能快照"},
        headers=headers,
    )
    run_id = uuid.UUID(resp.json()["id"])

    engine = VoyageEngine(event_bus=RecordingBus(), llm_router=LLMRouter())
    await engine.run(run_id)

    async with get_sessionmaker()() as session:
        run = await session.get(VoyageRun, run_id)
        assert run is not None
        snapshot = (run.checkpoint or {})["skills"]
    entry = snapshot["forge.score"][0]
    assert entry["slug"] == "my-scoring"
    assert entry["version"] == 1
    assert entry["body"] == SKILL_BODY
