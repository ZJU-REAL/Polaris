"""WS 通知端点：JWT 校验单元测试（完整 pub/sub 转发在部署环境联调）。"""

from app.api.ws import authenticate_ws_token
from tests.conftest import register_and_login


async def test_ws_token_auth(client):
    assert await authenticate_ws_token(None) is None
    assert await authenticate_ws_token("not-a-jwt") is None

    token = await register_and_login(client, email="ws@example.com")
    user = await authenticate_ws_token(token)
    assert user is not None
    assert user.email == "ws@example.com"
