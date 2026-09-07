"""Runner v2 接线（#716）：分派翻转 / 凭据放宽 / 资源租约接入实验链路。

三条线各取最小可信断言面：
- 分派：动作层「获得执行器」唯一入口（_resolve_runner_plugin）按
  checkpoint.params.backend 从注册表拿插件——fake 后端拿到 fake 插件，
  python-ml 拿到绑定 v1 底座（open_runner 返回物）的适配器；
- 凭据：ngspice（credential_kinds 为空）无凭据创建成功且 credential_id 落空；
  python-ml 无凭据仍被 schema 拒绝（422，行为不变）；
- 租约：experiment_setup 备环境前按 resource_id 排队获取（幂等、忙则可诊断
  失败），voyage 终态由 release_for_run 兜底释放（整管线集成验证）。
"""

import uuid

import pytest
import pytest_asyncio
from sqlalchemy import select

from app.agents.voyage import VoyageEngine
from app.agents.voyage import actions_experiment as ax
from app.agents.voyage.actions import ActionContext
from app.core.db import get_sessionmaker
from app.core.llm.router import LLMRouter
from app.models.experiment import Experiment
from app.models.idea import Idea
from app.models.resource import ResourceLease
from app.models.voyage import VoyageRun
from app.services import resource_leases, ssh_exec
from app.services.runners import registry as runner_registry
from app.services.runners.contract import RunnerManifest, RunStatus
from app.services.runners.python_ml import PythonMLRunner
from tests.conftest import RecordingBus, register_and_login
from tests.fake_ssh import FakeSSHConnector, FakeSSHServer
from tests.test_ssh_credentials import PAYLOAD as CRED_PAYLOAD

RUN_LOG = (
    'POLARIS_METRIC {"name": "accuracy", "step": 1, "value": 0.7}\n'
    "done (fake experiment)\n"
)
FAKE_PNG = b"\x89PNG\r\n\x1a\n(fake png bytes)"


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


async def _setup_project(client, email="alice@example.com"):
    token = await register_and_login(client, email)
    headers = {"Authorization": f"Bearer {token}"}
    resp = await client.post("/api/projects", json={"name": "wiring-proj"}, headers=headers)
    assert resp.status_code == 201
    return resp.json()["id"], headers


async def _seed_idea(project_id: str) -> str:
    async with get_sessionmaker()() as session:
        idea = Idea(
            project_id=uuid.UUID(project_id),
            title="runner v2 wiring idea",
            summary="s",
            status="promoted",
        )
        session.add(idea)
        await session.commit()
        return str(idea.id)


async def _create_credential(client, headers) -> str:
    resp = await client.post("/api/ssh-credentials", json=CRED_PAYLOAD, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _ctx_for(voyage: VoyageRun) -> ActionContext:
    return ActionContext(run=voyage, llm=LLMRouter(), checkpoint=dict(voyage.checkpoint or {}))


async def _load_exp_and_voyage(exp_id: str) -> tuple[Experiment, VoyageRun]:
    async with get_sessionmaker()() as session:
        experiment = await session.get(Experiment, uuid.UUID(exp_id))
        voyage = await session.get(VoyageRun, experiment.voyage_id)
        return experiment, voyage


# ---- 分派翻转 ----


class FakeEchoPlugin:
    """注册进注册表的假后端：只用于验证分派点拿到正确插件（无凭据、无底座）。"""

    manifest = RunnerManifest(
        backend="fake-echo",
        interaction="batch",
        side_effects="none",
        credential_kinds=(),
    )

    @classmethod
    def create(cls, **_):
        return cls()

    async def validate(self, plan):
        return []

    async def prepare(self, ctx):
        return None

    async def launch(self, ctx):
        return "fake-handle"

    async def poll(self, handle):
        return RunStatus(state="succeeded", exit_code=0)

    async def collect(self, ctx):
        raise NotImplementedError

    async def cancel(self, handle):
        return None

    async def cleanup(self, ctx):
        return None

    async def dry_run(self, ctx):
        raise NotImplementedError


async def test_dispatch_hands_out_registered_fake_plugin(client, queue_stub, monkeypatch):
    """backend=fake 的实验：创建无需凭据，动作层分派点拿到 fake 插件本尊。"""
    monkeypatch.setitem(runner_registry._FACTORIES, "fake-echo", FakeEchoPlugin.create)
    project_id, headers = await _setup_project(client)
    idea_id = await _seed_idea(project_id)

    resp = await client.post(
        f"/api/projects/{project_id}/experiments",
        json={"idea_id": idea_id, "params": {"backend": "fake-echo"}},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    exp = resp.json()
    assert exp["server_host"] is None

    experiment, voyage = await _load_exp_and_voyage(exp["id"])
    assert experiment.credential_id is None
    assert voyage.checkpoint["params"]["backend"] == "fake-echo"

    ctx = _ctx_for(voyage)
    async with get_sessionmaker()() as session:
        plugin, runner = await ax._resolve_runner_plugin(session, ctx, experiment)
        assert isinstance(plugin, FakeEchoPlugin)
        assert runner is None
        # 既有 19 原语流程只对 SSH 底座后端成立：非 SSH 后端明确报错（可诊断）
        with pytest.raises(ValueError, match="fake-echo"):
            await ax._open_executor(session, ctx, experiment)


async def test_dispatch_python_ml_binds_v1_substrate(client, queue_stub, fake_ssh):
    """缺省后端（python-ml）：插件绑定的底座就是 open_runner 的返回物（等价性）。"""
    project_id, headers = await _setup_project(client)
    idea_id = await _seed_idea(project_id)
    cred_id = await _create_credential(client, headers)
    resp = await client.post(
        f"/api/projects/{project_id}/experiments",
        json={"idea_id": idea_id, "credential_id": cred_id},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    experiment, voyage = await _load_exp_and_voyage(resp.json()["id"])
    assert voyage.checkpoint["params"]["backend"] == "python-ml"

    ctx = _ctx_for(voyage)
    async with get_sessionmaker()() as session:
        plugin, runner = await ax._resolve_runner_plugin(session, ctx, experiment)
        try:
            assert isinstance(plugin, PythonMLRunner)
            assert isinstance(runner, ssh_exec.SSHExecutor)  # v1 底座（裸机形态）
            assert plugin._runner is runner  # 适配器委托的正是同一底座
        finally:
            if runner is not None:
                await runner.close()


async def test_dispatch_unknown_backend_is_diagnosable(client, queue_stub):
    """checkpoint 里的 backend 事后失效（插件卸载）：UnknownBackendError，不静默回退。"""
    voyage = VoyageRun(
        kind="experiment",
        goal="g",
        status="executing",
        checkpoint={"params": {"backend": "gone-backend"}},
    )
    ctx = _ctx_for(voyage)
    with pytest.raises(runner_registry.UnknownBackendError):
        await ax._resolve_runner_plugin(None, ctx, None)


# ---- 凭据放宽 ----


async def test_ngspice_creates_without_credential(client, queue_stub):
    project_id, headers = await _setup_project(client)
    idea_id = await _seed_idea(project_id)
    resp = await client.post(
        f"/api/projects/{project_id}/experiments",
        json={"idea_id": idea_id, "params": {"backend": "ngspice"}},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    experiment, voyage = await _load_exp_and_voyage(resp.json()["id"])
    assert experiment.credential_id is None
    assert experiment.server_host is None
    assert voyage.checkpoint["params"]["backend"] == "ngspice"


async def test_python_ml_still_requires_credential(client, queue_stub):
    """存量行为不变：python-ml（显式或缺省）没有凭据/资源 → 422。"""
    project_id, headers = await _setup_project(client)
    idea_id = await _seed_idea(project_id)
    for payload in (
        {"idea_id": idea_id},
        {"idea_id": idea_id, "params": {"backend": "python-ml"}},
    ):
        resp = await client.post(
            f"/api/projects/{project_id}/experiments", json=payload, headers=headers
        )
        assert resp.status_code == 422, resp.text


async def test_non_ssh_backend_still_validates_given_credential(client, queue_stub):
    """后端用不上凭据 ≠ 错凭据放行：给了 credential_id 就照旧校验归属。"""
    project_id, headers = await _setup_project(client)
    idea_id = await _seed_idea(project_id)
    resp = await client.post(
        f"/api/projects/{project_id}/experiments",
        json={
            "idea_id": idea_id,
            "credential_id": str(uuid.uuid4()),
            "params": {"backend": "ngspice"},
        },
        headers=headers,
    )
    assert resp.status_code == 404, resp.text


# ---- 资源租约 ----


async def _register_host(client, headers, name="lease-box") -> str:
    cred_id = await _create_credential(client, headers)
    resp = await client.post(
        "/api/resources/runner-hosts",
        json={"name": name, "credential_id": cred_id},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _active_leases(resource_id: str) -> list[ResourceLease]:
    async with get_sessionmaker()() as session:
        rows = await session.execute(
            select(ResourceLease).where(
                ResourceLease.resource_id == uuid.UUID(resource_id),
                ResourceLease.released_at.is_(None),
            )
        )
        return list(rows.scalars().all())


async def test_setup_lease_acquire_idempotent_and_busy_diagnosable(
    client, queue_stub, monkeypatch
):
    headers = {
        "Authorization": f"Bearer {await register_and_login(client, 'lease@example.com')}"
    }
    resource_id = await _register_host(client, headers)

    async with get_sessionmaker()() as session:
        run1 = VoyageRun(
            kind="experiment",
            goal="r1",
            status="executing",
            checkpoint={"params": {"resource_id": resource_id}},
        )
        run2 = VoyageRun(
            kind="experiment",
            goal="r2",
            status="executing",
            checkpoint={"params": {"resource_id": resource_id}},
        )
        session.add_all([run1, run2])
        await session.commit()
        await session.refresh(run1)
        await session.refresh(run2)

    # 没写 resource_id 的实验：不进租约体系（缺省路径零变化）
    plain = VoyageRun(kind="experiment", goal="p", status="executing", checkpoint={"params": {}})
    await ax._acquire_resource_lease(_ctx_for(plain))
    assert await _active_leases(resource_id) == []

    # run1 获租；重入（setup 修复循环/断点续跑）不叠加
    await ax._acquire_resource_lease(_ctx_for(run1))
    assert len(await _active_leases(resource_id)) == 1
    await ax._acquire_resource_lease(_ctx_for(run1))
    leases = await _active_leases(resource_id)
    assert len(leases) == 1
    assert leases[0].run_id == run1.id

    # run2 排队直到超时：可诊断失败（ValueError），不是崩溃
    monkeypatch.setattr(ax, "RESOURCE_LEASE_WAIT_SECONDS", 0.05)
    with pytest.raises(ValueError, match="runner 主机忙"):
        await ax._acquire_resource_lease(_ctx_for(run2))

    # run1 终态释放（四个终态点都挂了 release_for_run）后 run2 立即可得
    async with get_sessionmaker()() as session:
        released = await resource_leases.release_for_run(session, run1.id)
        assert released == 1
    await ax._acquire_resource_lease(_ctx_for(run2))
    leases = await _active_leases(resource_id)
    assert len(leases) == 1
    assert leases[0].run_id == run2.id

    # 资源被删后：可诊断失败
    async with get_sessionmaker()() as session:
        gone = VoyageRun(
            kind="experiment",
            goal="g",
            status="executing",
            checkpoint={"params": {"resource_id": str(uuid.uuid4())}},
        )
        session.add(gone)
        await session.commit()
        await session.refresh(gone)
    with pytest.raises(ValueError, match="已不存在"):
        await ax._acquire_resource_lease(_ctx_for(gone))


async def test_pipeline_with_resource_id_acquires_then_releases_lease(
    client, queue_stub, fake_ssh, bus_recorder
):
    """整管线：resource_id 实验 setup 前获租、voyage 终态兜底释放（#680 模式复用）。"""
    project_id, headers = await _setup_project(client)
    idea_id = await _seed_idea(project_id)
    resource_id = await _register_host(client, headers, name="pipeline-box")

    resp = await client.post(
        f"/api/projects/{project_id}/experiments",
        json={"idea_id": idea_id, "resource_id": resource_id},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    exp = resp.json()
    voyage_id = uuid.UUID(exp["voyage_id"])

    async def leases_for_run():
        async with get_sessionmaker()() as session:
            rows = await session.execute(
                select(ResourceLease).where(ResourceLease.run_id == voyage_id)
            )
            return list(rows.scalars().all())

    assert await leases_for_run() == []  # 创建只记录 resource_id，不提前占席位

    engine = VoyageEngine(event_bus=RecordingBus(), llm_router=LLMRouter())
    await engine.run(voyage_id)

    resp = await client.get(f"/api/voyages/{voyage_id}", headers=headers)
    assert resp.json()["status"] == "done", resp.json()
    resp = await client.get(f"/api/experiments/{exp['id']}", headers=headers)
    assert resp.json()["status"] == "done"

    leases = await leases_for_run()
    assert len(leases) == 1  # setup 获租恰一次（幂等），终态已释放
    assert leases[0].released_at is not None
    assert leases[0].note == "experiment.setup"
