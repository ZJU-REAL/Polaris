"""钉钉/飞书群机器人：配置隔离、密文存储、签名与单向推送。"""

import json
import uuid

import httpx
from sqlalchemy import select

from app.core.db import get_sessionmaker
from app.core.security import decrypt_secret
from app.models.chat_bot import ChatBotConfig
from app.services import chat_bots
from tests.conftest import register_and_login


async def _auth(client, email: str = "alice@example.com") -> tuple[dict[str, str], uuid.UUID]:
    token = await register_and_login(client, email)
    headers = {"Authorization": f"Bearer {token}"}
    me = await client.get("/api/users/me", headers=headers)
    return headers, uuid.UUID(me.json()["id"])


def test_normalize_robot_id_accepts_only_fixed_official_webhooks():
    ding_token = "ding_token_0123456789"
    feishu_token = "f99fed8d-9b01-4dfe-ab56-0123456789ab"
    assert (
        chat_bots.normalize_robot_id(
            "dingtalk",
            f"https://oapi.dingtalk.com/robot/send?access_token={ding_token}",
        )
        == ding_token
    )
    assert (
        chat_bots.normalize_robot_id(
            "feishu", f"https://open.feishu.cn/open-apis/bot/v2/hook/{feishu_token}"
        )
        == feishu_token
    )
    assert chat_bots.normalize_robot_id("dingtalk", ding_token) == ding_token

    bad = (
        "http://oapi.dingtalk.com/robot/send?access_token=ding_token_0123456789",
        "https://evil.example/robot/send?access_token=ding_token_0123456789",
        "https://oapi.dingtalk.com.evil.example/robot/send?access_token=x01234567",
        "https://open.feishu.cn@evil.example/open-apis/bot/v2/hook/x01234567",
    )
    for value in bad:
        platform = "feishu" if "feishu" in value else "dingtalk"
        try:
            chat_bots.normalize_robot_id(platform, value)  # type: ignore[arg-type]
        except chat_bots.InvalidChatBotIdError:
            pass
        else:
            raise AssertionError(f"unsafe webhook accepted: {value}")


async def test_config_crud_is_encrypted_write_only_and_per_user(client):
    alice, alice_id = await _auth(client)
    bob, _ = await _auth(client, "bob@example.com")
    webhook = (
        "https://oapi.dingtalk.com/robot/send?"
        "access_token=ding_token_0123456789abcdef"
    )

    empty = await client.get("/api/chat-bots", headers=alice)
    assert empty.status_code == 200
    assert empty.json() == [
        {
            "platform": "dingtalk",
            "configured": False,
            "has_secret": False,
            "updated_at": None,
            "last_delivered_at": None,
        },
        {
            "platform": "feishu",
            "configured": False,
            "has_secret": False,
            "updated_at": None,
            "last_delivered_at": None,
        },
    ]

    saved = await client.put(
        "/api/chat-bots/dingtalk",
        headers=alice,
        json={"robot_id": webhook, "secret": "SEC-ding-signing-secret"},
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["configured"] is True
    assert saved.json()["has_secret"] is True
    assert "robot_id" not in saved.json() and "secret" not in saved.json()

    # 只更新 Webhook、不填写 Secret 时保留旧密钥，避免设置页的写-only 输入误清配置。
    updated = await client.put(
        "/api/chat-bots/dingtalk",
        headers=alice,
        json={"robot_id": "ding_token_replaced_0123456789"},
    )
    assert updated.status_code == 200
    assert updated.json()["has_secret"] is True

    async with get_sessionmaker()() as session:
        row = (
            await session.execute(
                select(ChatBotConfig).where(ChatBotConfig.user_id == alice_id)
            )
        ).scalar_one()
        assert "ding_token_0123456789abcdef" not in row.robot_id_encrypted
        assert "SEC-ding-signing-secret" not in (row.secret_encrypted or "")
        assert decrypt_secret(row.robot_id_encrypted) == "ding_token_replaced_0123456789"
        assert decrypt_secret(row.secret_encrypted or "") == "SEC-ding-signing-secret"

    # 另一用户既看不到，也不能借用 Alice 的配置发送。
    other = await client.get("/api/chat-bots", headers=bob)
    assert all(not item["configured"] for item in other.json())
    missing = await client.post(
        "/api/chat-bots/dingtalk/messages",
        headers=bob,
        json={"text": "should not send"},
    )
    assert missing.status_code == 409
    assert missing.json()["detail"] == "CHAT_BOT_NOT_CONFIGURED"

    deleted = await client.delete("/api/chat-bots/dingtalk", headers=alice)
    assert deleted.status_code == 204
    assert all(
        not item["configured"]
        for item in (await client.get("/api/chat-bots", headers=alice)).json()
    )


async def test_config_rejects_arbitrary_webhook_host(client):
    headers, _ = await _auth(client)
    response = await client.put(
        "/api/chat-bots/feishu",
        headers=headers,
        json={"robot_id": "https://attacker.example/open-apis/bot/v2/hook/abcdefgh"},
    )
    assert response.status_code == 422
    assert response.json()["detail"] == "INVALID_CHAT_BOT_ID"

    whitespace = await client.post(
        "/api/chat-bots/feishu/messages",
        headers=headers,
        json={"text": "   "},
    )
    assert whitespace.status_code == 422


async def test_dingtalk_delivery_uses_query_signature_and_markdown(client):
    headers, user_id = await _auth(client)
    await client.put(
        "/api/chat-bots/dingtalk",
        headers=headers,
        json={
            "robot_id": "ding_token_0123456789abcdef",
            "secret": "SEC-ding-signing-secret",
        },
    )
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"errcode": 0, "errmsg": "ok"})

    async with get_sessionmaker()() as session:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as upstream:
            parts = await chat_bots.deliver_message(
                session,
                user_id=user_id,
                platform="dingtalk",
                title="论文推荐",
                text="这篇论文值得阅读。",
                link="https://polaris.example/papers/1/read",
                client=upstream,
            )
        row = (
            await session.execute(
                select(ChatBotConfig).where(ChatBotConfig.user_id == user_id)
            )
        ).scalar_one()
        assert row.last_delivered_at is not None

    assert parts == 1 and len(requests) == 1
    request = requests[0]
    assert request.url.host == "oapi.dingtalk.com"
    assert request.url.params["access_token"] == "ding_token_0123456789abcdef"
    timestamp = int(request.url.params["timestamp"])
    assert request.url.params["sign"] == chat_bots._dingtalk_signature(  # noqa: SLF001
        "SEC-ding-signing-secret", timestamp
    )
    payload = json.loads(request.content)
    assert payload["msgtype"] == "markdown"
    assert payload["markdown"]["title"] == "论文推荐"
    assert "这篇论文值得阅读。" in payload["markdown"]["text"]
    assert "https://polaris.example/papers/1/read" in payload["markdown"]["text"]


async def test_feishu_delivery_uses_body_signature_and_post_link(client):
    headers, user_id = await _auth(client)
    hook_id = "f99fed8d-9b01-4dfe-ab56-0123456789ab"
    await client.put(
        "/api/chat-bots/feishu",
        headers=headers,
        json={"robot_id": hook_id, "secret": "feishu-signing-secret"},
    )
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"code": 0, "msg": "success", "data": {}})

    async with (
        get_sessionmaker()() as session,
        httpx.AsyncClient(transport=httpx.MockTransport(handler)) as upstream,
    ):
        parts = await chat_bots.deliver_message(
            session,
            user_id=user_id,
            platform="feishu",
            title="Polaris 分享",
            text="结论正文",
            link="https://polaris.example/papers/2/read",
            client=upstream,
        )

    assert parts == 1 and len(requests) == 1
    request = requests[0]
    assert request.url == f"https://open.feishu.cn/open-apis/bot/v2/hook/{hook_id}"
    payload = json.loads(request.content)
    timestamp = int(payload["timestamp"])
    assert payload["sign"] == chat_bots._feishu_signature(  # noqa: SLF001
        "feishu-signing-secret", timestamp
    )
    assert payload["msg_type"] == "post"
    post = payload["content"]["post"]["zh_cn"]
    assert post["content"][0][0] == {"tag": "text", "text": "结论正文"}
    assert post["content"][1][0]["href"] == "https://polaris.example/papers/2/read"
    assert len(request.content) < 20 * 1024


async def test_upstream_rejection_maps_to_safe_502(client, monkeypatch):
    headers, _ = await _auth(client)
    await client.put(
        "/api/chat-bots/feishu",
        headers=headers,
        json={"robot_id": "feishu_hook_0123456789"},
    )

    async def rejected(*_args, **_kwargs):
        raise chat_bots.ChatBotDeliveryError("upstream_rejected")

    monkeypatch.setattr(chat_bots, "deliver_message", rejected)
    response = await client.post(
        "/api/chat-bots/feishu/messages",
        headers=headers,
        json={"text": "hello"},
    )
    assert response.status_code == 502
    assert response.json()["detail"] == "CHAT_BOT_DELIVERY_FAILED:upstream_rejected"
    assert "feishu_hook_0123456789" not in response.text


def test_long_unicode_text_is_split_without_data_loss():
    text = ("研究结论一。\n" * 1800).strip()
    parts = chat_bots._split_text(text)  # noqa: SLF001
    assert len(parts) > 1
    assert all(len(part.encode("utf-8")) <= 12 * 1024 for part in parts)
    assert "".join(parts).replace("\n", "") == text.replace("\n", "")
