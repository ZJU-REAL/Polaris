"""技能系统 S2/S3 测试：persona 优先级 · 试运行 · workflow 展开与「运行此流程」。"""

import uuid

from app.agents.voyage.actions import ActionContext
from app.agents.voyage.actions_ideas import _personas as debate_personas
from app.agents.voyage.actions_review import _personas as referee_personas
from app.agents.voyage.engine import VoyageEngine
from app.agents.voyage.navigator import Navigator, _expand_workflow
from app.core.db import get_sessionmaker
from app.core.llm.base import CompletionResult, Message
from app.core.llm.router import LLMRouter
from app.models.voyage import VoyageRun
from tests.conftest import RecordingBus, register_and_login

PERSONA_SNAPSHOT = {
    "review.debate": [
        {
            "slug": "my-debaters",
            "kind": "persona",
            "version": 1,
            "personas": [
                {"name": "正方甲", "stance": "支持"},
                {"name": "反方乙", "stance": "反对"},
                {"name": "裁判丙", "stance": "中立"},
            ],
        }
    ],
    "review.referees": [
        {
            "slug": "my-referees",
            "kind": "persona",
            "version": 1,
            "personas": [{"name": "方法审稿人", "stance": "看方法"}],
        }
    ],
}

WORKFLOW_STEPS = [
    {
        "title": "分析",
        "action": "llm.complete",
        "params": {"stage": "default", "prompt": "分析 {goal}，关注 {focus}"},
        "acceptance": "输出分析",
        "requires_gate": None,
    },
    {
        "title": "总结",
        "action": "llm.complete",
        "params": {"stage": "default", "prompt": "总结 {goal}"},
        "acceptance": "输出总结",
        "requires_gate": None,
    },
]


def _ctx(checkpoint):
    return ActionContext(run=None, llm=None, checkpoint=checkpoint)  # type: ignore[arg-type]


# ---- persona 优先级：显式 params > persona 技能 > 内置默认 ----


def test_debate_personas_priority():
    pro, con, judge = debate_personas(_ctx({"skills": PERSONA_SNAPSHOT}))
    assert (pro["name"], con["name"], judge["name"]) == ("正方甲", "反方乙", "裁判丙")

    # 显式 params 优先于技能
    explicit = {
        "params": {"personas": [{"name": "指定正方", "stance": "s"}]},
        "skills": PERSONA_SNAPSHOT,
    }
    pro, _con, _judge = debate_personas(_ctx(explicit))
    assert pro["name"] == "指定正方"

    # 无技能无参数 → 内置默认三人设
    pro, con, judge = debate_personas(_ctx({}))
    assert all(p.get("name") for p in (pro, con, judge))


def test_referee_personas_priority():
    personas = referee_personas(_ctx({"skills": PERSONA_SNAPSHOT}))
    assert personas[0]["name"] == "方法审稿人"
    assert len(personas) == 3  # 不足三个用默认补齐


# ---- 试运行 ----


async def _setup(client):
    token = await register_and_login(client)
    headers = {"Authorization": f"Bearer {token}"}
    resp = await client.post("/api/projects", json={"name": "wf-proj"}, headers=headers)
    return headers, resp.json()["id"]


async def test_skill_test_run_guidance_and_persona(client):
    headers, _ = await _setup(client)
    rubric = (
        await client.post(
            "/api/skills",
            json={
                "slug": "t-rubric",
                "kind": "rubric",
                "name": "测试标准",
                "manifest": {"targets": ["forge.score"]},
                "body": "严格一点。",
            },
            headers=headers,
        )
    ).json()
    resp = await client.post(f"/api/skills/{rubric['id']}/test", json={}, headers=headers)
    assert resp.status_code == 200, resp.text
    result = resp.json()
    assert "测试标准" in result["rendered"] and "严格一点" in result["rendered"]
    assert result["output"].startswith("[fake:")  # 无路由回退 fake provider

    persona = (
        await client.post(
            "/api/skills",
            json={
                "slug": "t-personas",
                "kind": "persona",
                "name": "测试人设",
                "manifest": {
                    "targets": ["review.referees"],
                    "personas": [{"name": "甲", "stance": "s"}],
                },
                "body": "人设包。",
            },
            headers=headers,
        )
    ).json()
    result = (
        await client.post(f"/api/skills/{persona['id']}/test", json={}, headers=headers)
    ).json()
    assert result["output"] is None
    assert "甲" in result["rendered"]


# ---- workflow：运行此流程 ----


async def _create_workflow_skill(client, headers):
    resp = await client.post(
        "/api/skills",
        json={
            "slug": "my-flow",
            "kind": "workflow",
            "name": "两步流程",
            "manifest": {"targets": ["navigator.free_plan"], "steps": WORKFLOW_STEPS},
            "body": "两步分析流程。",
        },
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def test_run_workflow_skill_creates_and_drives_voyage(client, queue_stub, bus_recorder):
    headers, project_id = await _setup(client)
    skill = await _create_workflow_skill(client, headers)

    resp = await client.post(
        f"/api/skills/{skill['id']}/run",
        json={"project_id": project_id, "goal": "评估某方法", "vars": {"focus": "可复现性"}},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    voyage = resp.json()
    assert voyage["kind"] == "custom"
    assert [s["title"] for s in voyage["plan"]] == ["分析", "总结"]
    # 运行时变量并入步骤 params.vars（prompt 模板渲染用）
    assert voyage["plan"][0]["params"]["vars"] == {"focus": "可复现性"}
    assert ("run_voyage", (voyage["id"],), {}) in queue_stub.jobs

    # 引擎可以直接驱动到 done（fake LLM；plan 已就位，跳过 Navigator）
    engine = VoyageEngine(event_bus=RecordingBus(), llm_router=LLMRouter())
    await engine.run(uuid.UUID(voyage["id"]))
    async with get_sessionmaker()() as session:
        run = await session.get(VoyageRun, uuid.UUID(voyage["id"]))
        assert run.status == "done"
        assert (run.checkpoint or {})["params"]["skill_slug"] == "my-flow"

    # 非 workflow 技能不可运行；非成员项目 404
    rubric = (
        await client.post(
            "/api/skills",
            json={
                "slug": "not-flow",
                "kind": "rubric",
                "name": "非流程",
                "manifest": {"targets": ["forge.score"]},
                "body": "x",
            },
            headers=headers,
        )
    ).json()
    resp = await client.post(
        f"/api/skills/{rubric['id']}/run",
        json={"project_id": project_id, "goal": "g"},
        headers=headers,
    )
    assert resp.status_code == 422
    token_b = await register_and_login(client, email="bob@example.com")
    headers_b = {"Authorization": f"Bearer {token_b}"}
    flow_b = await client.post(
        "/api/skills",
        json={
            "slug": "my-flow",
            "kind": "workflow",
            "name": "两步流程",
            "manifest": {"targets": ["navigator.free_plan"], "steps": WORKFLOW_STEPS},
            "body": "b 的流程。",
        },
        headers=headers_b,
    )
    resp = await client.post(
        f"/api/skills/{flow_b.json()['id']}/run",
        json={"project_id": project_id, "goal": "g"},
        headers=headers_b,
    )
    assert resp.status_code == 404


# ---- workflow：Navigator use_skill 展开 ----


class _StubRouter:
    """navigator 规划专用：固定返回 use_skill 指令。"""

    def __init__(self, content: str):
        self._content = content
        self.calls: list[list[Message]] = []

    async def complete(self, stage, messages, **kwargs) -> CompletionResult:
        self.calls.append(list(messages))
        return CompletionResult(content=self._content, model="stub", usage={})


def test_expand_workflow_validates_steps():
    workflows = [{"slug": "my-flow", "name": "两步流程", "steps": WORKFLOW_STEPS}]
    steps = _expand_workflow("my-flow", workflows)
    assert [s["action"] for s in steps] == ["llm.complete", "llm.complete"]
    try:
        _expand_workflow("nope", workflows)
        raise AssertionError("should raise")
    except ValueError:
        pass


async def test_export_import_roundtrip(client):
    headers, _ = await _setup(client)
    skill = (
        await client.post(
            "/api/skills",
            json={
                "slug": "share-me",
                "kind": "rubric",
                "name": "可分享标准",
                "manifest": {"targets": ["forge.score"]},
                "body": "分享内容。",
            },
            headers=headers,
        )
    ).json()

    export = (await client.get(f"/api/skills/{skill['id']}/export", headers=headers)).json()
    assert export["format"] == "polaris-skill@1"
    assert export["body"] == "分享内容。"
    assert export["manifest"]["targets"] == ["forge.score"]

    # 自己导入：slug 冲突自动加后缀；内容全量保留
    resp = await client.post("/api/skills/import", json=export, headers=headers)
    assert resp.status_code == 201, resp.text
    imported = resp.json()
    assert imported["slug"] == "share-me-2"
    assert imported["current_version"]["body"] == "分享内容。"

    # 非法包被拒绝
    bad = dict(export, format="other@9")
    assert (await client.post("/api/skills/import", json=bad, headers=headers)).status_code == 422


async def test_voyage_detail_lists_snapshot_skills(client, queue_stub, bus_recorder):
    headers, project_id = await _setup(client)
    skill = (
        await client.post(
            "/api/skills",
            json={
                "slug": "detail-rubric",
                "kind": "rubric",
                "name": "详情页技能",
                "manifest": {"targets": ["forge.score"]},
                "body": "x",
            },
            headers=headers,
        )
    ).json()
    await client.post(
        f"/api/projects/{project_id}/skills",
        json={"skill_id": skill["id"], "target": "forge.score"},
        headers=headers,
    )
    resp = await client.post(
        "/api/voyages",
        json={"kind": "demo", "project_id": project_id, "goal": "看技能快照"},
        headers=headers,
    )
    run_id = resp.json()["id"]
    engine = VoyageEngine(event_bus=RecordingBus(), llm_router=LLMRouter())
    await engine.run(uuid.UUID(run_id))

    detail = (await client.get(f"/api/voyages/{run_id}", headers=headers)).json()
    assert detail["skills"] == [
        {
            "slug": "detail-rubric",
            "name": "详情页技能",
            "kind": "rubric",
            "version": 1,
            "target": "forge.score",
        }
    ]


async def test_navigator_free_plan_uses_workflow_skill():
    stub = _StubRouter('{"use_skill": "my-flow"}')
    navigator = Navigator(stub)  # type: ignore[arg-type]
    run = VoyageRun(
        kind="custom",
        goal="做一个综述",
        status="planning",
        cursor=0,
        checkpoint={
            "skills": {
                "navigator.free_plan": [
                    {
                        "slug": "my-flow",
                        "name": "两步流程",
                        "kind": "workflow",
                        "version": 1,
                        "steps": WORKFLOW_STEPS,
                    }
                ]
            }
        },
        project_id=uuid.uuid4(),
        created_by=None,
    )
    steps = await navigator.plan(run)
    assert [s["title"] for s in steps] == ["分析", "总结"]
    # 模板附录进了 system prompt
    system = stub.calls[0][0].content
    assert "my-flow" in system and "use_skill" in system
