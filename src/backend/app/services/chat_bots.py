"""群自定义机器人：用户配置管理、签名与单向 Webhook 推送。

仅允许钉钉/飞书官方 Webhook host，并从完整 URL 中提取 token 后按固定 endpoint
重建请求，避免用户可控 URL 形成 SSRF。机器人凭据由调用方在入库前/出库后加解密。
"""

import asyncio
import base64
import hashlib
import hmac
import json
import re
import time
import uuid
from collections.abc import Sequence
from urllib.parse import parse_qs, urlsplit

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import decrypt_secret, encrypt_secret
from app.models.base import utcnow
from app.models.chat_bot import ChatBotConfig
from app.schemas.chat_bot import ChatBotPlatform

DINGTALK_WEBHOOK = "https://oapi.dingtalk.com/robot/send"
FEISHU_WEBHOOK_PREFIX = "https://open.feishu.cn/open-apis/bot/v2/hook/"
_SAFE_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{8,512}$")
_FEISHU_PATH_RE = re.compile(r"^/open-apis/bot/v2/hook/([^/]+)$")
# 留出标题、链接、JSON 结构和签名字段的余量，确保飞书单请求小于官方 20 KB 上限。
_PART_TEXT_BYTES = 12 * 1024


class InvalidChatBotIdError(ValueError):
    pass


class ChatBotNotConfiguredError(LookupError):
    pass


class ChatBotDeliveryError(RuntimeError):
    """上游拒绝或网络失败；异常文本绝不包含 token / secret。"""

    def __init__(self, reason: str = "upstream_error") -> None:
        self.reason = reason
        super().__init__(reason)


def normalize_robot_id(platform: ChatBotPlatform, raw: str) -> str:
    """接受完整官方 Webhook 或裸 token，最终只保存 token。"""
    value = raw.strip()
    if value.startswith(("http://", "https://")):
        try:
            parsed = urlsplit(value)
            port = parsed.port
        except ValueError as exc:
            raise InvalidChatBotIdError from exc
        if parsed.scheme != "https" or parsed.username or parsed.password or port:
            raise InvalidChatBotIdError
        if platform == "dingtalk":
            if parsed.hostname != "oapi.dingtalk.com" or parsed.path != "/robot/send":
                raise InvalidChatBotIdError
            values = parse_qs(parsed.query, strict_parsing=False).get("access_token", [])
            if len(values) != 1:
                raise InvalidChatBotIdError
            value = values[0]
        else:
            if parsed.hostname != "open.feishu.cn" or parsed.query or parsed.fragment:
                raise InvalidChatBotIdError
            matched = _FEISHU_PATH_RE.fullmatch(parsed.path)
            if matched is None:
                raise InvalidChatBotIdError
            value = matched.group(1)
    if _SAFE_TOKEN_RE.fullmatch(value) is None:
        raise InvalidChatBotIdError
    return value


async def list_configs(
    session: AsyncSession, *, user_id: uuid.UUID
) -> Sequence[ChatBotConfig]:
    stmt = select(ChatBotConfig).where(ChatBotConfig.user_id == user_id)
    return (await session.execute(stmt)).scalars().all()


async def get_config(
    session: AsyncSession, *, user_id: uuid.UUID, platform: ChatBotPlatform
) -> ChatBotConfig | None:
    stmt = select(ChatBotConfig).where(
        ChatBotConfig.user_id == user_id,
        ChatBotConfig.platform == platform,
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def upsert_config(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    platform: ChatBotPlatform,
    robot_id: str,
    secret: str | None,
    replace_secret: bool = True,
) -> ChatBotConfig:
    normalized_id = normalize_robot_id(platform, robot_id)
    row = await get_config(session, user_id=user_id, platform=platform)
    if row is None:
        row = ChatBotConfig(user_id=user_id, platform=platform)
        session.add(row)
    row.robot_id_encrypted = encrypt_secret(normalized_id)
    # 更新 Webhook 时若请求没带 secret，保留已经保存的签名密钥；显式 null/空串才清除。
    if replace_secret or row.secret_encrypted is None:
        row.secret_encrypted = encrypt_secret(secret) if secret else None
    await session.commit()
    await session.refresh(row)
    return row


async def delete_config(session: AsyncSession, config: ChatBotConfig) -> None:
    await session.delete(config)
    await session.commit()


def _dingtalk_signature(secret: str, timestamp_ms: int) -> str:
    string_to_sign = f"{timestamp_ms}\n{secret}".encode()
    digest = hmac.new(secret.encode(), string_to_sign, digestmod=hashlib.sha256).digest()
    return base64.b64encode(digest).decode()


def _feishu_signature(secret: str, timestamp_s: int) -> str:
    # 飞书以 "timestamp\nsecret" 作为 HMAC key，对空字节串求 SHA-256。
    key = f"{timestamp_s}\n{secret}".encode()
    digest = hmac.new(key, b"", digestmod=hashlib.sha256).digest()
    return base64.b64encode(digest).decode()


def _split_text(text: str) -> list[str]:
    """按 UTF-8 字节切分，不截断 Unicode 字符；优先在换行处断开。"""
    chunks: list[str] = []
    remaining = text.strip()
    while len(remaining.encode("utf-8")) > _PART_TEXT_BYTES:
        used = 0
        cut = 0
        last_newline = -1
        last_newline_bytes = -1
        for index, char in enumerate(remaining):
            width = len(char.encode("utf-8"))
            if used + width > _PART_TEXT_BYTES:
                break
            used += width
            cut = index + 1
            if char == "\n":
                last_newline = cut
                last_newline_bytes = used
        if last_newline_bytes >= _PART_TEXT_BYTES // 3:
            cut = last_newline
        chunks.append(remaining[:cut].strip())
        remaining = remaining[cut:].strip()
    if remaining:
        chunks.append(remaining)
    return chunks


def _part_title(title: str, part: int, total: int) -> str:
    return title if total == 1 else f"{title}（{part}/{total}）"


def _dingtalk_payload(title: str, text: str, link: str | None) -> dict:
    body = text
    if link:
        body += f"\n\n[在 Polaris 中打开]({link})"
    return {"msgtype": "markdown", "markdown": {"title": title, "text": body}}


def _feishu_payload(
    title: str,
    text: str,
    link: str | None,
    *,
    secret: str | None,
    timestamp_s: int,
) -> dict:
    paragraphs: list[list[dict[str, str]]] = [[{"tag": "text", "text": text}]]
    if link:
        paragraphs.append([{"tag": "a", "text": "在 Polaris 中打开", "href": link}])
    payload: dict = {
        "msg_type": "post",
        "content": {"post": {"zh_cn": {"title": title, "content": paragraphs}}},
    }
    if secret:
        payload.update(
            {"timestamp": str(timestamp_s), "sign": _feishu_signature(secret, timestamp_s)}
        )
    return payload


def _upstream_success(platform: ChatBotPlatform, response: httpx.Response) -> bool:
    if not 200 <= response.status_code < 300:
        return False
    try:
        body = response.json()
    except (json.JSONDecodeError, ValueError):
        return False
    if not isinstance(body, dict):
        return False
    if platform == "dingtalk":
        return body.get("errcode") == 0
    return body.get("code", body.get("StatusCode")) == 0


async def _send_part(
    client: httpx.AsyncClient,
    *,
    platform: ChatBotPlatform,
    robot_id: str,
    secret: str | None,
    title: str,
    text: str,
    link: str | None,
) -> None:
    try:
        if platform == "dingtalk":
            timestamp_ms = int(time.time() * 1000)
            params = {"access_token": robot_id}
            if secret:
                params.update(
                    {
                        "timestamp": str(timestamp_ms),
                        "sign": _dingtalk_signature(secret, timestamp_ms),
                    }
                )
            response = await client.post(
                DINGTALK_WEBHOOK,
                params=params,
                json=_dingtalk_payload(title, text, link),
            )
        else:
            timestamp_s = int(time.time())
            response = await client.post(
                f"{FEISHU_WEBHOOK_PREFIX}{robot_id}",
                json=_feishu_payload(
                    title, text, link, secret=secret, timestamp_s=timestamp_s
                ),
            )
    except httpx.RequestError as exc:
        raise ChatBotDeliveryError("network_error") from exc
    if not _upstream_success(platform, response):
        raise ChatBotDeliveryError("upstream_rejected")


async def deliver_message(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    platform: ChatBotPlatform,
    title: str,
    text: str,
    link: str | None,
    client: httpx.AsyncClient | None = None,
) -> int:
    config = await get_config(session, user_id=user_id, platform=platform)
    if config is None:
        raise ChatBotNotConfiguredError

    robot_id = decrypt_secret(config.robot_id_encrypted)
    secret = decrypt_secret(config.secret_encrypted) if config.secret_encrypted else None
    parts = _split_text(text)
    owns_client = client is None
    http_client = client or httpx.AsyncClient(
        timeout=httpx.Timeout(10.0),
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    try:
        for index, part in enumerate(parts, start=1):
            await _send_part(
                http_client,
                platform=platform,
                robot_id=robot_id,
                secret=secret,
                title=_part_title(title, index, len(parts)),
                text=part,
                link=link,
            )
            # 多段消息稍作间隔，兼容飞书 5 次/秒的单机器人频控。
            if index < len(parts):
                await asyncio.sleep(0.22)
    finally:
        if owns_client:
            await http_client.aclose()

    config.last_delivered_at = utcnow()
    await session.commit()
    return len(parts)
