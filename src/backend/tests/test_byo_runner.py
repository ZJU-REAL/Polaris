"""BYO runner tier-1（#685）：agent 版本钉定 / 主机注册与租约 / 凭据吊销 / 密钥不出库。"""

import uuid

import pytest
import pytest_asyncio
from sqlalchemy import select

from app.core.db import get_sessionmaker
from app.core.security import encrypt_secret
from app.models.experiment import Experiment
from app.models.idea import Idea
from app.models.resource import Resource
from app.models.ssh_credential import SSHCredential
from app.models.voyage import VoyageRun
from app.services import byo_runner, resource_leases, ssh_exec
from tests.conftest import register_and_login
from tests.fake_ssh import FakeSSHConnector, FakeSSHServer, FakeSSHSession
from tests.test_ssh_credentials import PAYLOAD as CRED_PAYLOAD

FAKE_PEM = CRED_PAYLOAD["private_key"]


@pytest_asyncio.fixture
async def fake_ssh(app):
    server = FakeSSHServer()
    ssh_exec.set_connector_factory(lambda: FakeSSHConnector(server))
    yield server
    ssh_exec.set_connector_factory(None)


async def _auth(client, email="alice@example.com"):
    token = await register_and_login(client, email)
    return {"Authorization": f"Bearer {token}"}


async def _create_credential(client, headers) -> str:
    resp = await client.post("/api/ssh-credentials", json=CRED_PAYLOAD, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _agent_file_count(server: FakeSSHServer) -> int:
    return sum(1 for path in server.files if path.startswith(".polaris-runner/"))


# ---- agent 版本钉定 ----


async def test_ensure_agent_pushes_then_skips(app):
    server = FakeSSHServer()
    session = FakeSSHSession(server)

    # 首连：无 VERSION → 整包推送（脚本 + 版本号）
    assert await byo_runner.ensure_agent(session) is True
    for name in byo_runner.AGENT_SCRIPTS:
        assert f".polaris-runner/agent/{name}" in server.files
    assert server.host_files["~/.polaris-runner/VERSION"].strip() == byo_runner.AGENT_VERSION
    pushed = _agent_file_count(server)

    # 后连：版本一致 → 一条 cat 即跳过，不再写文件
    commands_before = len(server.commands)
    assert await byo_runner.ensure_agent(session) is False
    assert len(server.commands) == commands_before + 1  # 只有 cat VERSION
    assert _agent_file_count(server) == pushed


async def test_ensure_agent_repushes_on_stale_version(app):
    server = FakeSSHServer()
    server.host_files["~/.polaris-runner/VERSION"] = "0\n"  # 过期版本
    session = FakeSSHSession(server)
    assert await byo_runner.ensure_agent(session) is True
    assert server.host_files["~/.polaris-runner/VERSION"].strip() == byo_runner.AGENT_VERSION


async def test_open_executor_auto_ensures_agent(fake_ssh):
    """open_executor 连上即保证 agent 就位（首连推送）；agent 脚本是固定文本。"""
    credential = SSHCredential(
        user_id=uuid.uuid4(),
        name="box",
        host="gpu1.lab",
        port=22,
        username="polaris",
        private_key_encrypted=encrypt_secret(FAKE_PEM),
    )
    executor = await ssh_exec.open_executor(
        credential=credential, exp_id=str(uuid.uuid4()), project_id=uuid.uuid4()
    )
    assert _agent_file_count(fake_ssh) == len(byo_runner.AGENT_SCRIPTS)
    await executor.close()


# ---- runner 主机注册（host 类 Resource）与租约 ----


async def test_register_runner_host_api(client):
    headers = await _auth(client)
    cred_id = await _create_credential(client, headers)

    resp = await client.post(
        "/api/resources/runner-hosts",
        json={"name": "lab-a100", "credential_id": cred_id},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["kind"] == "host" and body["exclusive"] is True
    assert body["credential_id"] == cred_id
    assert body["config"]["ephemeral"] is True  # 默认推荐容器化 ephemeral 执行

    # 显式接受 non-ephemeral（裸机直跑）
    resp = await client.post(
        "/api/resources/runner-hosts",
        json={"name": "bare-box", "credential_id": cred_id, "ephemeral": False},
        headers=headers,
    )
    assert resp.status_code == 201
    assert resp.json()["config"]["ephemeral"] is False

    # 他人凭据 → 404（不泄露存在性）
    headers_b = await _auth(client, "bob@example.com")
    resp = await client.post(
        "/api/resources/runner-hosts",
        json={"name": "steal", "credential_id": cred_id},
        headers=headers_b,
    )
    assert resp.status_code == 404

    # 非 ssh 凭据 → 422（tier-1 只吃 SSH 直连）
    resp = await client.post(
        "/api/connection-credentials",
        json={"name": "token", "kind": "http", "host": "api.lab", "payload": {"token": "t"}},
        headers=headers,
    )
    http_cred = resp.json()["id"]
    resp = await client.post(
        "/api/resources/runner-hosts",
        json={"name": "not-ssh", "credential_id": http_cred},
        headers=headers,
    )
    assert resp.status_code == 422


async def test_runner_host_lease_mutual_exclusion(client):
    """注册出来的 host 资源天然进 #680 租约体系：默认独占，第二个 run 拿不到。"""
    headers = await _auth(client)
    cred_id = await _create_credential(client, headers)
    resp = await client.post(
        "/api/resources/runner-hosts",
        json={"name": "one-at-a-time", "credential_id": cred_id},
        headers=headers,
    )
    resource_id = uuid.UUID(resp.json()["id"])

    async with get_sessionmaker()() as session:
        resource = await session.get(Resource, resource_id)
        run1 = VoyageRun(kind="experiment", goal="r1", status="executing")
        run2 = VoyageRun(kind="experiment", goal="r2", status="executing")
        session.add_all([run1, run2])
        await session.commit()
        await session.refresh(run1)
        await session.refresh(run2)
        lease = await resource_leases.acquire(session, resource, run1)
        assert lease.released_at is None
        with pytest.raises(resource_leases.ResourceBusyError):
            await resource_leases.acquire(session, resource, run2)


# ---- 凭据吊销联动 ----


async def _seed_active_experiment(client, headers, credential_id: str) -> uuid.UUID:
    """直接落一行非终态实验引用凭据（不跑完整创建流程，吊销联动只看引用）。"""
    resp = await client.post("/api/projects", json={"name": "revoke-proj"}, headers=headers)
    project_id = uuid.UUID(resp.json()["id"])
    async with get_sessionmaker()() as session:
        idea = Idea(project_id=project_id, title="idea", summary="s", status="promoted")
        session.add(idea)
        await session.flush()
        experiment = Experiment(
            project_id=project_id,
            idea_id=idea.id,
            credential_id=uuid.UUID(credential_id),
            status="running",
        )
        session.add(experiment)
        await session.commit()
        return experiment.id


async def test_revoke_credential_with_active_run_conflicts(client):
    headers = await _auth(client)
    cred_id = await _create_credential(client, headers)
    resp = await client.post(
        "/api/resources/runner-hosts",
        json={"name": "box", "credential_id": cred_id},
        headers=headers,
    )
    resource_id = uuid.UUID(resp.json()["id"])
    exp_id = await _seed_active_experiment(client, headers, cred_id)

    # 活跃引用：老端点与通用端点都 409，凭据仍在
    for url in (f"/api/ssh-credentials/{cred_id}", f"/api/connection-credentials/{cred_id}"):
        resp = await client.delete(url, headers=headers)
        assert resp.status_code == 409, resp.text
        assert resp.json()["detail"] == "CREDENTIAL_IN_USE"
    resp = await client.get("/api/ssh-credentials", headers=headers)
    assert [c["id"] for c in resp.json()] == [cred_id]

    # 实验进终态后可吊销；host 资源标记不可用且脱钩凭据（资源本身保留）
    async with get_sessionmaker()() as session:
        experiment = await session.get(Experiment, exp_id)
        experiment.status = "done"
        await session.commit()
    resp = await client.delete(f"/api/ssh-credentials/{cred_id}", headers=headers)
    assert resp.status_code == 204, resp.text
    async with get_sessionmaker()() as session:
        resource = await session.get(Resource, resource_id)
        assert resource is not None
        assert resource.credential_id is None
        assert resource.config["unavailable"] is True
        assert resource.config["unavailable_reason"] == "credential_revoked"
        assert resource.config["ephemeral"] is True  # 原有配置保留


# ---- 实验创建：resource_id 指定 runner 主机 ----


async def _seed_promoted_idea(client, headers) -> tuple[str, str]:
    resp = await client.post("/api/projects", json={"name": "byo-proj"}, headers=headers)
    project_id = resp.json()["id"]
    async with get_sessionmaker()() as session:
        idea = Idea(
            project_id=uuid.UUID(project_id), title="byo idea", summary="s", status="promoted"
        )
        session.add(idea)
        await session.commit()
        return project_id, str(idea.id)


async def test_experiment_create_with_resource_id(client, queue_stub):
    headers = await _auth(client)
    cred_id = await _create_credential(client, headers)
    resp = await client.post(
        "/api/resources/runner-hosts",
        json={"name": "target-box", "credential_id": cred_id},
        headers=headers,
    )
    resource_id = resp.json()["id"]
    project_id, idea_id = await _seed_promoted_idea(client, headers)

    resp = await client.post(
        f"/api/projects/{project_id}/experiments",
        json={"idea_id": idea_id, "resource_id": resource_id},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    exp_id = uuid.UUID(resp.json()["id"])
    async with get_sessionmaker()() as session:
        experiment = await session.get(Experiment, exp_id)
        # 凭据从资源上解析而来；resource_id 记入 voyage checkpoint 供后续租约消费
        assert str(experiment.credential_id) == cred_id
        voyage = await session.get(VoyageRun, experiment.voyage_id)
        assert voyage.checkpoint["params"]["resource_id"] == resource_id


async def test_experiment_create_resource_id_errors(client, queue_stub):
    headers = await _auth(client)
    cred_id = await _create_credential(client, headers)
    project_id, idea_id = await _seed_promoted_idea(client, headers)

    # 两者都不给 → 422（schema 校验）
    resp = await client.post(
        f"/api/projects/{project_id}/experiments", json={"idea_id": idea_id}, headers=headers
    )
    assert resp.status_code == 422

    # 不存在的资源 → 404
    resp = await client.post(
        f"/api/projects/{project_id}/experiments",
        json={"idea_id": idea_id, "resource_id": str(uuid.uuid4())},
        headers=headers,
    )
    assert resp.status_code == 404
    assert resp.json()["detail"] == "RESOURCE_NOT_FOUND"

    # 凭据吊销后的资源（unavailable）→ 409
    resp = await client.post(
        "/api/resources/runner-hosts",
        json={"name": "will-revoke", "credential_id": cred_id},
        headers=headers,
    )
    resource_id = resp.json()["id"]
    resp = await client.delete(f"/api/ssh-credentials/{cred_id}", headers=headers)
    assert resp.status_code == 204
    resp = await client.post(
        f"/api/projects/{project_id}/experiments",
        json={"idea_id": idea_id, "resource_id": resource_id},
        headers=headers,
    )
    assert resp.status_code == 409
    assert resp.json()["detail"] == "RESOURCE_UNAVAILABLE"


# ---- 密钥不出库：连接失败的异常文本不得携带私钥 ----


class LeakyConnector:
    """模拟「底层库把输入私钥嵌进异常文本」的最坏情况（asyncssh 实际不会）。"""

    async def connect(self, *, host, port, username, private_key, passphrase=None):
        raise ConnectionError(f"handshake rejected, offending key: {private_key}")


async def test_connect_failure_detail_never_contains_private_key(client):
    headers = await _auth(client)
    cred_id = await _create_credential(client, headers)
    async with get_sessionmaker()() as session:
        credential = (
            await session.execute(
                select(SSHCredential).where(SSHCredential.id == uuid.UUID(cred_id))
            )
        ).scalar_one()
        # 触发 lazy 属性加载后脱离会话使用（test_credential 只读属性）
        assert credential.private_key_encrypted

    ssh_exec.set_connector_factory(LeakyConnector)
    try:
        ok, detail = await ssh_exec.test_credential(credential)
        assert ok is False
        assert "fake-key-material" not in detail
        assert "PRIVATE KEY" not in detail
        assert "[REDACTED]" in detail

        info = await ssh_exec.probe_sysinfo(credential)
        assert info["ok"] is False
        assert "fake-key-material" not in info["detail"]
        assert "PRIVATE KEY" not in info["detail"]
    finally:
        ssh_exec.set_connector_factory(None)


async def test_scrub_secrets_covers_partial_lines():
    """异常只嵌 PEM 某一行也要被抹掉（整段 replace 不够）。"""
    text = "error near fake-key-material in input"
    scrubbed = ssh_exec.scrub_secrets(text, FAKE_PEM, None)
    assert "fake-key-material" not in scrubbed
    assert "[REDACTED]" in scrubbed
