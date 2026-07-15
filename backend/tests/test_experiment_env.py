"""实验环境注入测试：评测模型 llm_config.json、env.sh（HF 镜像）、白名单模板
source 前缀、prompt 条件段（eval_model / hf_mirror / extra_notes）与 params 透传。

与 test_experiments.py 同套离线基建（fake LLM + fake SSH），复用其项目/闸门辅助函数。
"""

import json
import uuid

import pytest_asyncio
from sqlalchemy import select

from app.agents.voyage import actions_experiment as ax
from app.core.db import get_sessionmaker
from app.core.llm.fake import FakeProvider
from app.core.llm.router import LLMRouter
from app.core.security import encrypt_secret
from app.models.activity import Activity
from app.models.llm_config import LLMProviderConfig, ModelRoute
from app.models.voyage import VoyageRun
from app.services import ssh_exec
from tests.fake_ssh import FakeSSHConnector, FakeSSHServer
from tests.test_experiments import (
    FAKE_PNG,
    RUN_LOG,
    _approve_gate,
    _create_credential,
    _make_engine,
    _seed_idea,
    _setup_project,
)

EVAL_MODEL = "qwen36-35b-a3b"
EVAL_BASE_URL = "https://llm.example.com/v1"
EVAL_API_KEY = "sk-eval-secret-1234567890abcdef"
ENV_PREFIX = "[ -f env.sh ] && . ./env.sh;"


@pytest_asyncio.fixture
async def fake_ssh(app):
    server = FakeSSHServer(
        run_log=RUN_LOG,
        plot_outputs={
            "figures/primary_metric.png": FAKE_PNG,
            "figures/primary_metric.pdf": b"%PDF-1.4 (fake pdf)",
        },
    )
    ssh_exec.set_connector_factory(lambda: FakeSSHConnector(server))
    yield server
    ssh_exec.set_connector_factory(None)


@pytest_asyncio.fixture(autouse=True)
def fast_poll(monkeypatch):
    monkeypatch.setattr(ax, "RUN_POLL_SECONDS", 0)


async def _seed_default_route() -> None:
    """default stage 路由到带 base_url + 加密 key 的 fake provider
    （kind=fake 保证离线；resolve 出的 route 仍携带解密后的 api_key）。"""
    async with get_sessionmaker()() as session:
        provider = LLMProviderConfig(
            name="eval-provider",
            kind="fake",
            base_url=EVAL_BASE_URL,
            api_key_encrypted=encrypt_secret(EVAL_API_KEY),
            enabled=True,
        )
        session.add(provider)
        await session.flush()
        session.add(ModelRoute(stage="default", provider_id=provider.id, model="fake-default"))
        await session.commit()


async def _create_with_params(client, headers, project_id, idea_id, cred_id, params):
    resp = await client.post(
        f"/api/projects/{project_id}/experiments",
        json={"idea_id": idea_id, "credential_id": cred_id, "params": params},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _run_pipeline(client, headers, project_id, voyage_id, router=None):
    engine, _ = _make_engine(router)
    await engine.run(uuid.UUID(voyage_id))
    await _approve_gate(client, headers, project_id)
    await engine.resume(uuid.UUID(voyage_id))
    resp = await client.get(f"/api/voyages/{voyage_id}", headers=headers)
    assert resp.json()["status"] == "done", resp.json()


class _RecordingProvider(FakeProvider):
    """记录全部 prompt 的 fake provider（断言 system prompt 条件段注入用）。"""

    def __init__(self) -> None:
        self.prompts: list[str] = []

    async def complete(self, messages, *, model, temperature=0.7, max_tokens=None, images=None):
        self.prompts.append("\n".join(m.content for m in messages))
        return await super().complete(
            messages, model=model, temperature=temperature, max_tokens=max_tokens, images=images
        )


async def test_eval_model_injects_llm_config(client, queue_stub, fake_ssh, bus_recorder):
    """eval_model 非空：default 路由解析出的 base_url + 解密 api_key + eval_model
    写成 workdir/llm_config.json；审计里只有路径与字节数，key 不落任何 Activity。"""
    await _seed_default_route()
    project_id, headers = await _setup_project(client)
    idea_id = await _seed_idea(project_id)
    cred_id = await _create_credential(client, headers)
    exp = await _create_with_params(
        client, headers, project_id, idea_id, cred_id, {"eval_model": EVAL_MODEL}
    )
    await _run_pipeline(client, headers, project_id, exp["voyage_id"])

    config = json.loads(fake_ssh.files[f"polaris_runs/{exp['id']}/llm_config.json"])
    assert config == {"base_url": EVAL_BASE_URL, "api_key": EVAL_API_KEY, "model": EVAL_MODEL}

    async with get_sessionmaker()() as session:
        activities = (await session.execute(select(Activity))).scalars().all()
        assert activities
        for activity in activities:  # key 打码：解密 key 不出现在任何审计记录
            assert EVAL_API_KEY not in (activity.message or "")
            assert EVAL_API_KEY not in json.dumps(activity.payload or {}, ensure_ascii=False)
        # llm_config.json 的写入本身有 sftp:write 审计（只记路径与字节数）
        ssh_cmds = [a.payload["command"] for a in activities if a.kind == "ssh.exec"]
        assert any(c.startswith("sftp:write") and "llm_config.json" in c for c in ssh_cmds)


async def test_hf_mirror_env_sh(client, queue_stub, fake_ssh, bus_recorder):
    """hf_mirror=true：env.sh 含 HF_ENDPOINT 镜像 + 恒定 POLARIS_WORKDIR；
    未指定 eval_model 时不写 llm_config.json。"""
    project_id, headers = await _setup_project(client)
    idea_id = await _seed_idea(project_id)
    cred_id = await _create_credential(client, headers)
    exp = await _create_with_params(
        client, headers, project_id, idea_id, cred_id, {"hf_mirror": True}
    )
    await _run_pipeline(client, headers, project_id, exp["voyage_id"])

    env_sh = fake_ssh.files[f"polaris_runs/{exp['id']}/env.sh"]
    assert "export POLARIS_WORKDIR=$(pwd)" in env_sh
    assert "export HF_ENDPOINT=https://hf-mirror.com" in env_sh
    assert f"polaris_runs/{exp['id']}/llm_config.json" not in fake_ssh.files


async def test_command_templates_source_env(client, queue_stub, fake_ssh, bus_recorder):
    """默认参数：env.sh 只含 POLARIS_WORKDIR；smoke/launch/plot 三个白名单模板
    执行前都先 source env.sh（固定前缀，无可变参数）。"""
    project_id, headers = await _setup_project(client)
    idea_id = await _seed_idea(project_id)
    cred_id = await _create_credential(client, headers)
    exp = await _create_with_params(client, headers, project_id, idea_id, cred_id, {})
    await _run_pipeline(client, headers, project_id, exp["voyage_id"])

    assert fake_ssh.files[f"polaris_runs/{exp['id']}/env.sh"] == "export POLARIS_WORKDIR=$(pwd)\n"
    assert f"polaris_runs/{exp['id']}/llm_config.json" not in fake_ssh.files

    workdir = f"~/polaris_runs/{exp['id']}"
    smoke = next(c for c in fake_ssh.commands if "--smoke" in c)
    assert smoke == f"cd {workdir} && {{ {ENV_PREFIX} bash run.sh --smoke; }}"
    launch = next(c for c in fake_ssh.commands if "nohup" in c)
    assert f"nohup bash -c '{ENV_PREFIX} bash run.sh > run.log 2>&1; echo $? > run.exit'" in launch
    plot = next(c for c in fake_ssh.commands if "plot_figures.py" in c and ".venv" in c)
    assert plot == f"cd {workdir} && {{ {ENV_PREFIX} .venv/bin/python plot_figures.py; }}"


async def test_prompt_context_sections(client, queue_stub, fake_ssh, bus_recorder):
    """eval_model / hf_mirror / extra_notes：plan 与 codegen 的 system prompt
    条件追加对应段落；报告等其他 prompt 不受影响。"""
    notes = "务必对比 ReAct 基线并汇报 token 消耗（用户补充说明）"
    project_id, headers = await _setup_project(client)
    idea_id = await _seed_idea(project_id)
    cred_id = await _create_credential(client, headers)
    exp = await _create_with_params(
        client,
        headers,
        project_id,
        idea_id,
        cred_id,
        {"eval_model": EVAL_MODEL, "hf_mirror": True, "extra_notes": notes},
    )

    provider = _RecordingProvider()
    router = LLMRouter()
    router._providers[("fake", None, "")] = provider
    await _run_pipeline(client, headers, project_id, exp["voyage_id"], router)

    plan_prompts = [p for p in provider.prompts if "实验规划师" in p]
    code_prompts = [p for p in provider.prompts if "实验工程师" in p]
    assert plan_prompts and code_prompts
    for prompt in plan_prompts + code_prompts:
        assert "llm_config.json" in prompt  # 评测模型段
        assert "max_tokens≥2048" in prompt
        assert "HF_ENDPOINT" in prompt  # HF 镜像段
        assert notes in prompt  # 补充说明原文
    report_prompts = [p for p in provider.prompts if "报告撰写人" in p]
    assert report_prompts
    assert all("llm_config.json" not in p for p in report_prompts)


def test_prompt_with_context_unit():
    """条件段单元测试：无参数时 prompt 原样；各开关独立生效。"""

    def ctx(params):
        return ax.ActionContext(run=None, llm=None, checkpoint={"params": params})

    base = ax.CODE_SYSTEM_PROMPT
    assert ax._prompt_with_context(base, ctx({})) == base
    assert ax._prompt_with_context(base, ctx({"eval_model": "  ", "extra_notes": ""})) == base

    with_eval = ax._prompt_with_context(base, ctx({"eval_model": EVAL_MODEL}))
    assert with_eval.startswith(base) and "llm_config.json" in with_eval
    assert "HF_ENDPOINT" not in with_eval

    with_all = ax._prompt_with_context(
        base, ctx({"eval_model": EVAL_MODEL, "hf_mirror": True, "extra_notes": "N-1 说明"})
    )
    assert "llm_config.json" in with_all
    assert "https://hf-mirror.com" in with_all
    assert "N-1 说明" in with_all


async def test_params_passthrough(client, queue_stub, fake_ssh):
    """create_experiment 把 eval_model / hf_mirror / extra_notes 透传进
    voyage checkpoint.params；缺省时为 None / False / None。"""
    project_id, headers = await _setup_project(client)
    idea_id = await _seed_idea(project_id)
    cred_id = await _create_credential(client, headers)

    exp = await _create_with_params(
        client,
        headers,
        project_id,
        idea_id,
        cred_id,
        {
            "gpu_hint": "1×A100",
            "eval_model": EVAL_MODEL,
            "hf_mirror": True,
            "extra_notes": "只跑 ALFWorld 前 30 个任务",
        },
    )
    async with get_sessionmaker()() as session:
        voyage = await session.get(VoyageRun, uuid.UUID(exp["voyage_id"]))
        params = voyage.checkpoint["params"]
        assert params["experiment_id"] == exp["id"]
        assert params["gpu_hint"] == "1×A100"
        assert params["eval_model"] == EVAL_MODEL
        assert params["hf_mirror"] is True
        assert params["extra_notes"] == "只跑 ALFWorld 前 30 个任务"

    exp_default = await _create_with_params(client, headers, project_id, idea_id, cred_id, {})
    async with get_sessionmaker()() as session:
        voyage = await session.get(VoyageRun, uuid.UUID(exp_default["voyage_id"]))
        params = voyage.checkpoint["params"]
        assert params["eval_model"] is None
        assert params["hf_mirror"] is False
        assert params["extra_notes"] is None
