"""插件管理代理（#754）：谁能调、没配内核时什么表现、内核错误怎么透出。

这一层不实现插件语义（那在 Node 内核里，由 kernel 套件把关），它只负责一件事——
**在服务器上装插件 = 在服务端进程里跑第三方代码，对所有账号生效**，所以门槛必须是
主人而不是「已登录」。下面钉的就是这条线。
"""

import httpx
import pytest

from app.core.config import get_settings
from tests.conftest import register_and_login

PROBES = [
    ("POST", "/api/plugins/rpc", {"method": "plugins.list"}),
    ("GET", "/api/plugins/events", None),
]


@pytest.fixture(autouse=True)
def _kernel_configured(monkeypatch):
    """默认给一个内核地址；不配的用例自己覆盖回 None。"""
    settings = get_settings()
    monkeypatch.setattr(settings, "kernel_url", "http://kernel.invalid:8770", raising=False)
    monkeypatch.setattr(settings, "kernel_token", "unit-test-token", raising=False)
    yield


async def test_second_user_cannot_manage_plugins(client):
    await register_and_login(client, email="owner@example.com")
    second = await register_and_login(client, email="second@example.com")
    headers = {"Authorization": f"Bearer {second}"}

    for method, url, payload in PROBES:
        resp = await client.request(method, url, json=payload, headers=headers)
        # 装插件影响整个部署，不是「我自己的机器」——非主人一律 403
        assert resp.status_code == 403, (url, resp.status_code)
        assert resp.json()["detail"] == "OWNER_REQUIRED"


async def test_anonymous_cannot_manage_plugins(client):
    for method, url, payload in PROBES:
        resp = await client.request(method, url, json=payload)
        assert resp.status_code == 401, (url, resp.status_code)


async def test_unconfigured_kernel_reads_as_unavailable_not_broken(client, monkeypatch):
    token = await register_and_login(client, email="owner@example.com")
    settings = get_settings()
    monkeypatch.setattr(settings, "kernel_url", None, raising=False)

    resp = await client.post(
        "/api/plugins/rpc",
        json={"method": "plugins.list"},
        headers={"Authorization": f"Bearer {token}"},
    )
    # 「这个部署没接内核」与「内核挂了」必须可区分，前端据此决定显不显示插件页
    assert resp.status_code == 503
    assert resp.json()["detail"] == "KERNEL_NOT_CONFIGURED"


async def test_owner_call_forwards_with_the_shared_token(client, monkeypatch):
    token = await register_and_login(client, email="owner@example.com")
    seen: dict = {}

    class _Response:
        status_code = 200

        @staticmethod
        def json():
            return {"result": [{"id": "sources", "state": "active"}]}

    class _Client:
        def __init__(self, *_args, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

        async def post(self, url, json=None, headers=None):
            seen["url"] = url
            seen["json"] = json
            seen["headers"] = headers
            return _Response()

    monkeypatch.setattr(httpx, "AsyncClient", _Client)

    resp = await client.post(
        "/api/plugins/rpc",
        json={"method": "plugins.list"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    assert resp.json() == [{"id": "sources", "state": "active"}]
    assert seen["url"] == "http://kernel.invalid:8770/rpc"
    # 密钥只在服务端之间流动，浏览器永远看不到它
    assert seen["headers"]["x-polaris-kernel-token"] == "unit-test-token"
    assert seen["json"] == {"method": "plugins.list"}


async def test_kernel_method_failure_keeps_its_status_and_message(client, monkeypatch):
    token = await register_and_login(client, email="owner@example.com")

    class _Response:
        status_code = 400

        @staticmethod
        def json():
            return {"error": "integrity-mismatch: sha512 did not match"}

    class _Client:
        def __init__(self, *_args, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

        async def post(self, *_args, **_kwargs):
            return _Response()

    monkeypatch.setattr(httpx, "AsyncClient", _Client)

    resp = await client.post(
        "/api/plugins/rpc",
        json={"method": "plugins.market.install", "params": {"name": "x", "version": "1.0.0"}},
        headers={"Authorization": f"Bearer {token}"},
    )
    # 安装失败是业务结果：错误码要原样到前端，好按 MarketError 分流展示
    assert resp.status_code == 400
    assert "integrity-mismatch" in resp.json()["detail"]


async def test_unreachable_kernel_is_a_gateway_error(client, monkeypatch):
    token = await register_and_login(client, email="owner@example.com")

    class _Client:
        def __init__(self, *_args, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

        async def post(self, *_args, **_kwargs):
            raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(httpx, "AsyncClient", _Client)

    resp = await client.post(
        "/api/plugins/rpc",
        json={"method": "plugins.list"},
        headers={"Authorization": f"Bearer {token}"},
    )
    # 连不上是运行故障，与「这次调用不对」分开：502 而不是 400/503
    assert resp.status_code == 502
    assert "KERNEL_UNREACHABLE" in resp.json()["detail"]
