"""资源/租约/多态凭据（R2 #677）：CRUD、互斥与容量计数、终态兜底、凭据加密往返。"""

import asyncio
import json
import uuid

import pytest
from sqlalchemy import select

from app.core.db import get_sessionmaker
from app.core.security import decrypt_secret
from app.models.resource import Resource, ResourceLease
from app.models.ssh_credential import ConnectionCredential
from app.models.user import User
from app.models.voyage import VoyageRun
from app.services import resource_leases as leases_service
from app.services import voyages as voyages_service
from tests.conftest import register_and_login

FAKE_PEM = "-----BEGIN OPENSSH PRIVATE KEY-----\nfake\n-----END OPENSSH PRIVATE KEY-----\n"


async def _auth(client, email="alice@example.com"):
    token = await register_and_login(client, email)
    return {"Authorization": f"Bearer {token}"}


async def _user_id(email="alice@example.com") -> uuid.UUID:
    async with get_sessionmaker()() as session:
        return (
            await session.execute(select(User.id).where(User.email == email))
        ).scalar_one()


async def _make_run(session) -> VoyageRun:
    run = VoyageRun(kind="experiment", goal="lease-test", status="executing")
    session.add(run)
    await session.commit()
    await session.refresh(run)
    return run


async def _make_resource(session, owner_id, **overrides) -> Resource:
    fields = {"name": "gpu-box", "kind": "host", "capacity": 1, "exclusive": True}
    fields.update(overrides)
    resource = Resource(owner_id=owner_id, **fields)
    session.add(resource)
    await session.commit()
    await session.refresh(resource)
    return resource


async def _active_lease_count(resource_id) -> int:
    async with get_sessionmaker()() as session:
        return await leases_service.count_active_leases(session, resource_id)


# ---- 资源 CRUD API ----


async def test_resource_crud_api(client):
    headers = await _auth(client)

    resp = await client.post(
        "/api/resources",
        json={"name": "lab-a100", "kind": "host", "config": {"workdir": "~/runs"}},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    created = resp.json()
    assert created["kind"] == "host" and created["capacity"] == 1 and created["exclusive"]
    resource_id = created["id"]

    resp = await client.get("/api/resources", headers=headers)
    assert [r["id"] for r in resp.json()] == [resource_id]

    resp = await client.patch(
        f"/api/resources/{resource_id}",
        json={"name": "lab-a100-renamed", "capacity": 4, "exclusive": False},
        headers=headers,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "lab-a100-renamed" and body["capacity"] == 4

    resp = await client.delete(f"/api/resources/{resource_id}", headers=headers)
    assert resp.status_code == 204
    resp = await client.get("/api/resources", headers=headers)
    assert resp.json() == []


async def test_resource_kind_and_config_validation(client):
    headers = await _auth(client)

    # 未知 kind → pydantic Literal 校验 422
    resp = await client.post(
        "/api/resources", json={"name": "x", "kind": "spaceship"}, headers=headers
    )
    assert resp.status_code == 422

    # queue 缺 config.queue → 422（服务层轻校验）
    resp = await client.post(
        "/api/resources", json={"name": "slurm", "kind": "queue"}, headers=headers
    )
    assert resp.status_code == 422
    assert "config.queue" in resp.text

    # license_pool 带 feature 才放行
    resp = await client.post(
        "/api/resources",
        json={
            "name": "hfss-pool",
            "kind": "license_pool",
            "capacity": 3,
            "exclusive": False,
            "config": {"feature": "HFSS", "server": "lic.lab:1055"},
        },
        headers=headers,
    )
    assert resp.status_code == 201, resp.text

    # PATCH 换 config 也走同一校验（换掉 feature 键 → 422）
    resource_id = resp.json()["id"]
    resp = await client.patch(
        f"/api/resources/{resource_id}", json={"config": {"server": "x"}}, headers=headers
    )
    assert resp.status_code == 422


async def test_resource_owner_isolation(client):
    headers_a = await _auth(client, "alice@example.com")
    headers_b = await _auth(client, "bob@example.com")
    resp = await client.post(
        "/api/resources", json={"name": "mine", "kind": "host"}, headers=headers_a
    )
    resource_id = resp.json()["id"]

    resp = await client.get("/api/resources", headers=headers_b)
    assert resp.json() == []
    for method, url in (
        ("get", f"/api/resources/{resource_id}"),
        ("delete", f"/api/resources/{resource_id}"),
    ):
        resp = await getattr(client, method)(url, headers=headers_b)
        assert resp.status_code == 404
    resp = await client.patch(
        f"/api/resources/{resource_id}", json={"name": "hijack"}, headers=headers_b
    )
    assert resp.status_code == 404


async def test_resource_credential_ref_must_be_owned(client):
    headers_a = await _auth(client, "alice@example.com")
    headers_b = await _auth(client, "bob@example.com")
    resp = await client.post(
        "/api/connection-credentials",
        json={"name": "b-token", "kind": "http", "host": "api.lab", "payload": {"token": "t"}},
        headers=headers_b,
    )
    other_cred = resp.json()["id"]

    # 引用他人凭据 → 404（不泄露存在性）
    resp = await client.post(
        "/api/resources",
        json={"name": "x", "kind": "host", "credential_id": other_cred},
        headers=headers_a,
    )
    assert resp.status_code == 404


# ---- 多态凭据：各 kind 加密往返 ----


async def test_generic_credential_roundtrip_encrypted(client):
    headers = await _auth(client)
    cases = {
        "grpc": {"token": "grpc-secret", "tls_ca": "-----BEGIN CERT-----"},
        "http": {"token": "http-secret", "headers": {"X-Lab": "1"}},
        "visa": {"resource": "TCPIP0::10.0.0.5::INSTR", "secret": "pin"},
    }
    ids = {}
    for kind, payload in cases.items():
        resp = await client.post(
            "/api/connection-credentials",
            json={"name": f"{kind}-cred", "kind": kind, "host": "10.0.0.5", "payload": payload},
            headers=headers,
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        ids[kind] = body["id"]
        # 响应绝不含载荷/密钥
        assert "payload" not in body and "payload_encrypted" not in body
        assert body["kind"] == kind

    async with get_sessionmaker()() as session:
        for kind, payload in cases.items():
            row = await session.get(ConnectionCredential, uuid.UUID(ids[kind]))
            assert row.kind == kind and row.private_key_encrypted is None
            raw = row.payload_encrypted
            assert raw is not None and "secret" not in raw
            assert json.loads(decrypt_secret(raw)) == payload

    # kind 过滤
    resp = await client.get("/api/connection-credentials?kind=visa", headers=headers)
    assert [c["id"] for c in resp.json()] == [ids["visa"]]
    resp = await client.get("/api/connection-credentials", headers=headers)
    assert len(resp.json()) == 3


async def test_generic_credential_ssh_lands_in_legacy_columns(client):
    """通用端点建 ssh：私钥/口令进存量专列（ssh_exec 消费方零改动）。"""
    headers = await _auth(client)
    resp = await client.post(
        "/api/connection-credentials",
        json={
            "name": "gpu1",
            "kind": "ssh",
            "host": "gpu1.lab",
            "username": "polaris",
            "payload": {"private_key": FAKE_PEM, "passphrase": "s3cret"},
        },
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    async with get_sessionmaker()() as session:
        row = await session.get(ConnectionCredential, uuid.UUID(resp.json()["id"]))
        assert row.kind == "ssh" and row.payload_encrypted is None
        assert decrypt_secret(row.private_key_encrypted) == FAKE_PEM
        assert decrypt_secret(row.passphrase_encrypted) == "s3cret"

    # 缺 private_key → 422
    resp = await client.post(
        "/api/connection-credentials",
        json={"name": "bad", "kind": "ssh", "host": "h", "username": "u", "payload": {}},
        headers=headers,
    )
    assert resp.status_code == 422

    # visa 缺 resource → 422
    resp = await client.post(
        "/api/connection-credentials",
        json={"name": "bad", "kind": "visa", "host": "h", "payload": {"secret": "x"}},
        headers=headers,
    )
    assert resp.status_code == 422


async def test_legacy_ssh_rows_default_kind(client):
    """老端点建的凭据 kind='ssh'，通用端点列表看得到（同一张表）。"""
    headers = await _auth(client)
    resp = await client.post(
        "/api/ssh-credentials",
        json={
            "name": "legacy",
            "host": "gpu1.lab",
            "port": 22,
            "username": "polaris",
            "private_key": FAKE_PEM,
        },
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    resp = await client.get("/api/connection-credentials?kind=ssh", headers=headers)
    assert [c["name"] for c in resp.json()] == ["legacy"]


# ---- 租约：互斥 / 容量 / 释放 / 终态兜底 ----


async def test_exclusive_lease_busy_then_wait_acquire(client):
    headers = await _auth(client)
    del headers  # 只为造出 users 行
    owner = await _user_id()
    maker = get_sessionmaker()
    async with maker() as session:
        resource = await _make_resource(session, owner, exclusive=True)
        run1 = await _make_run(session)
        run2 = await _make_run(session)
        lease1 = await leases_service.acquire(session, resource, run1)
        assert lease1.released_at is None
        # 抄标量：下面的失败 acquire 会 rollback，把会话内对象全部 expire
        resource_id, run2_id, lease1_id = resource.id, run2.id, lease1.id

        # 独占资源已有活租约：wait=False 立刻 Busy
        with pytest.raises(leases_service.ResourceBusyError):
            await leases_service.acquire(session, resource, run2)

    # wait=True：先占着，0.05s 后由另一会话释放，等位者应拿到
    async def _release_soon():
        await asyncio.sleep(0.05)
        async with maker() as s2:
            lease = await s2.get(ResourceLease, lease1_id)
            await leases_service.release(s2, lease)

    async with maker() as session:
        resource = await session.get(Resource, resource_id)
        run2 = await session.get(VoyageRun, run2_id)
        releaser = asyncio.create_task(_release_soon())
        lease2 = await leases_service.acquire(
            session, resource, run2, wait=True, timeout=5.0
        )
        await releaser
        assert lease2.run_id == run2_id
    assert await _active_lease_count(resource_id) == 1


async def test_wait_and_acquire_times_out(client):
    headers = await _auth(client)
    del headers
    owner = await _user_id()
    async with get_sessionmaker()() as session:
        resource = await _make_resource(session, owner)
        run1 = await _make_run(session)
        run2 = await _make_run(session)
        await leases_service.acquire(session, resource, run1)
        with pytest.raises(leases_service.ResourceBusyError):
            await leases_service.wait_and_acquire(
                session, resource, run2, timeout=0.2, poll_interval=0.05
            )


async def test_capacity_counting_semantics(client):
    """capacity=2 非独占：两个活租约可并存，第三个 Busy；释放一个又能进。"""
    headers = await _auth(client)
    del headers
    owner = await _user_id()
    async with get_sessionmaker()() as session:
        resource = await _make_resource(
            session, owner, kind="license_pool", capacity=2, exclusive=False,
            config={"feature": "HFSS"},
        )
        runs = [await _make_run(session) for _ in range(3)]
        lease_a = await leases_service.acquire(session, resource, runs[0])
        await leases_service.acquire(session, resource, runs[1])
        # 抄标量：下面的失败 acquire 会 rollback，把会话内对象全部 expire
        resource_id, run3_id, lease_a_id = resource.id, runs[2].id, lease_a.id
        with pytest.raises(leases_service.ResourceBusyError):
            await leases_service.acquire(session, resource, runs[2])

        # rollback 后重取（异步会话摸 expired 属性会炸 MissingGreenlet）
        resource = await session.get(Resource, resource_id)
        run3 = await session.get(VoyageRun, run3_id)
        lease_a = await session.get(ResourceLease, lease_a_id)
        await leases_service.release(session, lease_a)
        lease_c = await leases_service.acquire(session, resource, run3)
        assert lease_c.released_at is None
        assert await leases_service.count_active_leases(session, resource_id) == 2


async def test_concurrent_acquire_single_winner(client):
    """并发安全：两个会话同时抢独占资源，只允许一个成功。"""
    headers = await _auth(client)
    del headers
    owner = await _user_id()
    maker = get_sessionmaker()
    async with maker() as session:
        resource = await _make_resource(session, owner)
        run1 = await _make_run(session)
        run2 = await _make_run(session)
        resource_id, run1_id, run2_id = resource.id, run1.id, run2.id

    async def _attempt(run_id):
        async with maker() as s:
            resource = await s.get(Resource, resource_id)
            run = await s.get(VoyageRun, run_id)
            try:
                await leases_service.acquire(s, resource, run)
                return "ok"
            except leases_service.ResourceBusyError:
                return "busy"

    results = await asyncio.gather(_attempt(run1_id), _attempt(run2_id))
    assert sorted(results) == ["busy", "ok"]
    assert await _active_lease_count(resource_id) == 1


async def test_release_idempotent_and_release_for_run(client):
    headers = await _auth(client)
    del headers
    owner = await _user_id()
    async with get_sessionmaker()() as session:
        r1 = await _make_resource(session, owner)
        r2 = await _make_resource(session, owner, name="q", kind="queue",
                                  exclusive=False, capacity=2, config={"queue": "gpu"})
        run = await _make_run(session)
        lease = await leases_service.acquire(session, r1, run)
        await leases_service.acquire(session, r2, run)

        await leases_service.release(session, lease)
        first_released_at = lease.released_at
        await leases_service.release(session, lease)  # 幂等
        assert lease.released_at == first_released_at

        # run 终态兜底：剩下的活租约一次清光；再调一次是 no-op
        assert await leases_service.release_for_run(session, run.id) == 1
        assert await leases_service.release_for_run(session, run.id) == 0
        assert await leases_service.count_active_leases(session, r1.id) == 0
        assert await leases_service.count_active_leases(session, r2.id) == 0


async def test_cancel_voyage_releases_leases(client):
    """取消任务（不经引擎的终态写入）也会兜底释放租约。"""
    headers = await _auth(client)
    del headers
    owner = await _user_id()
    async with get_sessionmaker()() as session:
        resource = await _make_resource(session, owner)
        run = await _make_run(session)
        await leases_service.acquire(session, resource, run)
        assert await leases_service.count_active_leases(session, resource.id) == 1

        await voyages_service.cancel_voyage(session, run)
        assert run.status == "cancelled"
        assert await leases_service.count_active_leases(session, resource.id) == 0
