"""LLM 调用日志：开关读取（短 TTL 缓存）、请求脱敏/截断、落库与保留期清理。

- 开关存 system_settings 表（key=llm_call_logging_enabled），~15s 进程内缓存，
  打开/关闭免重启即生效；管理端改动后调用 invalidate_flag_cache() 立即刷新。
- 记录尽力而为：任何失败只 log warning，绝不影响 LLM 主流程。
- 图片绝不存 base64，替换为 "[image ~N KB]" 占位；超长内容截断并标注。
- 每次写入时低频（默认 10 分钟一次）顺带删除 7 天前的旧日志。
"""

import logging
import time
import uuid
from collections.abc import Sequence
from datetime import timedelta
from typing import Any

from sqlalchemy import delete, select

from app.core.db import get_sessionmaker
from app.core.llm.base import Message
from app.models.base import utcnow

logger = logging.getLogger(__name__)

LLM_CALL_LOGGING_KEY = "llm_call_logging_enabled"

FLAG_TTL = 15.0  # 开关缓存秒数
MESSAGE_MAX_CHARS = 20_000  # 单条消息内容截断阈值
RESPONSE_MAX_CHARS = 50_000  # 响应全文截断阈值
SUMMARY_MAX_CHARS = 2_000  # embed/rerank 摘要里首条输入的截断阈值
RETENTION_DAYS = 7
_CLEANUP_INTERVAL = 600.0  # 保留期清理最小间隔（秒）

_flag_value = False
_flag_loaded_at: float | None = None
_last_cleanup_at: float | None = None


def reset_state() -> None:
    """测试用：清空开关缓存与清理节流状态。"""
    global _flag_value, _flag_loaded_at, _last_cleanup_at
    _flag_value = False
    _flag_loaded_at = None
    _last_cleanup_at = None


def invalidate_flag_cache() -> None:
    """管理端改动开关后调用，下次读取直接查库。"""
    global _flag_loaded_at
    _flag_loaded_at = None


async def logging_enabled() -> bool:
    """调用日志开关（~15s 缓存；查库失败视为关闭）。"""
    global _flag_value, _flag_loaded_at
    now = time.monotonic()
    if _flag_loaded_at is not None and now - _flag_loaded_at < FLAG_TTL:
        return _flag_value
    try:
        from app.models.system_setting import SystemSetting

        async with get_sessionmaker()() as session:
            value = (
                await session.execute(
                    select(SystemSetting.value).where(SystemSetting.key == LLM_CALL_LOGGING_KEY)
                )
            ).scalar_one_or_none()
        _flag_value = bool(value)
    except Exception:  # noqa: BLE001 — 开关读取失败视为关闭，不影响主流程
        logger.warning("llm call log flag read failed", exc_info=True)
        _flag_value = False
    _flag_loaded_at = now
    return _flag_value


def truncate_text(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n…[truncated, {len(text)} chars total]"


def sanitize_request(
    messages: Sequence[Message], images: list[bytes] | None = None
) -> dict[str, Any]:
    """messages → 可入库 JSON：内容截断到 MESSAGE_MAX_CHARS，图片只留大小占位。"""
    payload: dict[str, Any] = {
        "messages": [
            {"role": m.role, "content": truncate_text(m.content, MESSAGE_MAX_CHARS)}
            for m in messages
        ]
    }
    if images:
        payload["images"] = [f"[image ~{max(1, len(b) // 1024)} KB]" for b in images]
    return payload


async def record_call(
    *,
    stage: str,
    provider_name: str,
    model: str,
    duration_ms: int,
    status: str,
    error: str | None = None,
    request: Any | None = None,
    response: str | None = None,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    user_id: uuid.UUID | None = None,
    project_id: uuid.UUID | None = None,
    voyage_id: uuid.UUID | None = None,
) -> None:
    """写一条调用日志（尽力而为，失败只 warning）；低频顺带清理过期日志。"""
    global _last_cleanup_at
    try:
        from app.models.llm_config import LLMCallLog

        async with get_sessionmaker()() as session:
            now = time.monotonic()
            if _last_cleanup_at is None or now - _last_cleanup_at >= _CLEANUP_INTERVAL:
                _last_cleanup_at = now
                await session.execute(
                    delete(LLMCallLog).where(
                        LLMCallLog.created_at < utcnow() - timedelta(days=RETENTION_DAYS)
                    )
                )
            session.add(
                LLMCallLog(
                    stage=stage,
                    provider_name=provider_name,
                    model=model,
                    duration_ms=duration_ms,
                    status=status,
                    error=truncate_text(error, MESSAGE_MAX_CHARS) if error else None,
                    request=request,
                    response=truncate_text(response, RESPONSE_MAX_CHARS)
                    if response is not None
                    else None,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    user_id=user_id,
                    project_id=project_id,
                    voyage_id=voyage_id,
                )
            )
            await session.commit()
    except Exception:  # noqa: BLE001 — 日志记录绝不影响业务调用
        logger.warning("llm call log write failed (stage=%s)", stage, exc_info=True)
