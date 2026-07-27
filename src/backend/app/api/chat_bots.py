"""本人群机器人配置与单向消息推送 API。"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import current_active_user
from app.core.db import get_session
from app.models.chat_bot import ChatBotConfig
from app.models.user import User
from app.schemas.chat_bot import (
    ChatBotConfigRead,
    ChatBotConfigUpsert,
    ChatBotDeliveryRead,
    ChatBotMessageCreate,
    ChatBotPlatform,
)
from app.services import chat_bots as chat_bots_service

router = APIRouter(prefix="/chat-bots", tags=["chat-bots"])
_PLATFORMS: tuple[ChatBotPlatform, ...] = ("dingtalk", "feishu")


def _read(platform: ChatBotPlatform, row: ChatBotConfig | None) -> ChatBotConfigRead:
    return ChatBotConfigRead(
        platform=platform,
        configured=row is not None,
        has_secret=bool(row and row.secret_encrypted),
        updated_at=row.updated_at if row else None,
        last_delivered_at=row.last_delivered_at if row else None,
    )


@router.get("", response_model=list[ChatBotConfigRead])
async def list_configs(
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> list[ChatBotConfigRead]:
    rows = await chat_bots_service.list_configs(session, user_id=user.id)
    by_platform = {row.platform: row for row in rows}
    return [_read(platform, by_platform.get(platform)) for platform in _PLATFORMS]


@router.put("/{platform}", response_model=ChatBotConfigRead)
async def save_config(
    platform: ChatBotPlatform,
    body: ChatBotConfigUpsert,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> ChatBotConfigRead:
    try:
        row = await chat_bots_service.upsert_config(
            session,
            user_id=user.id,
            platform=platform,
            robot_id=body.robot_id,
            secret=body.secret,
            replace_secret="secret" in body.model_fields_set,
        )
    except chat_bots_service.InvalidChatBotIdError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, detail="INVALID_CHAT_BOT_ID"
        ) from exc
    return _read(platform, row)


@router.delete("/{platform}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_config(
    platform: ChatBotPlatform,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> None:
    row = await chat_bots_service.get_config(session, user_id=user.id, platform=platform)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="CHAT_BOT_NOT_CONFIGURED")
    await chat_bots_service.delete_config(session, row)


@router.post("/{platform}/messages", response_model=ChatBotDeliveryRead)
async def send_message(
    platform: ChatBotPlatform,
    body: ChatBotMessageCreate,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> ChatBotDeliveryRead:
    try:
        parts = await chat_bots_service.deliver_message(
            session,
            user_id=user.id,
            platform=platform,
            title=body.title,
            text=body.text,
            link=body.link,
        )
    except chat_bots_service.ChatBotNotConfiguredError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="CHAT_BOT_NOT_CONFIGURED") from exc
    except chat_bots_service.ChatBotDeliveryError as exc:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, detail=f"CHAT_BOT_DELIVERY_FAILED:{exc.reason}"
        ) from exc
    return ChatBotDeliveryRead(platform=platform, delivered=True, parts=parts)


@router.post("/{platform}/test", response_model=ChatBotDeliveryRead)
async def test_config(
    platform: ChatBotPlatform,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> ChatBotDeliveryRead:
    """显式测试会向目标群发送一条测试消息。"""
    body = ChatBotMessageCreate(
        title="Polaris 连接测试",
        text="Polaris 群机器人已连接成功，之后可在文献对话中通过 @ 选择此机器人分享回答。",
    )
    return await send_message(platform, body, session, user)
