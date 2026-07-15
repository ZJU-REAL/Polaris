"""SSH 凭据 API：CRUD + 加密落库 + 权限（非本人 404）+ test 端点（MockSSH）。"""

import uuid

import pytest_asyncio
from sqlalchemy import select

from app.core.db import get_sessionmaker
from app.core.security import decrypt_secret
from app.models.ssh_credential import SSHCredential
from app.services import ssh_exec
from tests.conftest import register_and_login
from tests.fake_ssh import FakeSSHConnector, FakeSSHServer

FAKE_PEM = (
    "-----BEGIN OPENSSH PRIVATE KEY-----\nfake-key-material\n-----END OPENSSH PRIVATE KEY-----\n"
)

PAYLOAD = {
    "name": "gpu-node-1",
    "host": "gpu1.lab.internal",
    "port": 22022,
    "username": "polaris",
    "private_key": FAKE_PEM,
    "passphrase": "s3cret",
}


@pytest_asyncio.fixture
async def fake_ssh(app):
    server = FakeSSHServer()
    ssh_exec.set_connector_factory(lambda: FakeSSHConnector(server))
    yield server
    ssh_exec.set_connector_factory(None)


async def _auth(client, email="alice@example.com"):
    token = await register_and_login(client, email)
    return {"Authorization": f"Bearer {token}"}


async def test_credential_crud_and_encryption(client):
    headers = await _auth(client)

    resp = await client.post("/api/ssh-credentials", json=PAYLOAD, headers=headers)
    assert resp.status_code == 201, resp.text
    created = resp.json()
    cred_id = created["id"]
    # 响应绝不含私钥/口令
    assert set(created) == {
        "id",
        "name",
        "host",
        "port",
        "username",
        "created_at",
        "last_verified_at",
        "proxy_url",
    }
    assert created["last_verified_at"] is None

    # 加密落库：密文 != 明文，且可解密还原
    async with get_sessionmaker()() as session:
        row = (
            await session.execute(
                select(SSHCredential).where(SSHCredential.id == uuid.UUID(cred_id))
            )
        ).scalar_one()
        assert row.private_key_encrypted != FAKE_PEM
        assert FAKE_PEM not in row.private_key_encrypted
        assert decrypt_secret(row.private_key_encrypted) == FAKE_PEM
        assert row.passphrase_encrypted is not None
        assert decrypt_secret(row.passphrase_encrypted) == "s3cret"

    resp = await client.get("/api/ssh-credentials", headers=headers)
    assert [c["id"] for c in resp.json()] == [cred_id]

    # 删除后列表为空
    resp = await client.delete(f"/api/ssh-credentials/{cred_id}", headers=headers)
    assert resp.status_code == 204
    resp = await client.get("/api/ssh-credentials", headers=headers)
    assert resp.json() == []


async def test_credential_isolation_between_users(client):
    headers_a = await _auth(client, "alice@example.com")
    headers_b = await _auth(client, "bob@example.com")

    resp = await client.post("/api/ssh-credentials", json=PAYLOAD, headers=headers_a)
    cred_id = resp.json()["id"]

    # 他人列表看不到、删不掉、测不了（404 不泄露存在性）
    resp = await client.get("/api/ssh-credentials", headers=headers_b)
    assert resp.json() == []
    resp = await client.delete(f"/api/ssh-credentials/{cred_id}", headers=headers_b)
    assert resp.status_code == 404
    resp = await client.post(f"/api/ssh-credentials/{cred_id}/test", headers=headers_b)
    assert resp.status_code == 404
    # 本人仍在
    resp = await client.get("/api/ssh-credentials", headers=headers_a)
    assert len(resp.json()) == 1


async def test_credential_test_endpoint_success(client, fake_ssh):
    headers = await _auth(client)
    resp = await client.post("/api/ssh-credentials", json=PAYLOAD, headers=headers)
    cred_id = resp.json()["id"]

    resp = await client.post(f"/api/ssh-credentials/{cred_id}/test", headers=headers)
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "detail": "ok"}
    # 连接参数来自凭据（解密后），echo ok 走白名单模板
    assert fake_ssh.connects == [("gpu1.lab.internal", 22022, "polaris")]
    assert "echo ok" in fake_ssh.commands
    # last_verified_at 已更新
    resp = await client.get("/api/ssh-credentials", headers=headers)
    assert resp.json()[0]["last_verified_at"] is not None


async def test_credential_test_endpoint_failure(client, fake_ssh):
    headers = await _auth(client)
    resp = await client.post("/api/ssh-credentials", json=PAYLOAD, headers=headers)
    cred_id = resp.json()["id"]

    fake_ssh.connect_error = "connection refused"
    resp = await client.post(f"/api/ssh-credentials/{cred_id}/test", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert "connection refused" in body["detail"]
    resp = await client.get("/api/ssh-credentials", headers=headers)
    assert resp.json()[0]["last_verified_at"] is None
