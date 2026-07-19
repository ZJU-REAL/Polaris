"""stdio 传输入口：``python -m app.mcp``，给本地 MCP 客户端（如 Claude Desktop）。

逐行读 stdin 的 JSON-RPC 消息，经 ``dispatch.handle_rpc`` 处理后把响应逐行写 stdout。
本地进程视为可信：用户由环境变量 ``POLARIS_MCP_USER_EMAIL`` 指定（该用户须已注册）；
每个工具调用仍需在参数里带 project_id，服务端照常校验成员身份。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid

from sqlalchemy import select

from app.core.db import get_sessionmaker
from app.mcp.dispatch import handle_rpc
from app.models.user import User


async def _resolve_user_id() -> uuid.UUID:
    email = os.environ.get("POLARIS_MCP_USER_EMAIL")
    if not email:
        raise SystemExit("需设置环境变量 POLARIS_MCP_USER_EMAIL（MCP 请求以该用户身份执行）")
    async with get_sessionmaker()() as session:
        user = (
            await session.execute(select(User).where(User.email == email))
        ).scalar_one_or_none()
    if user is None:
        raise SystemExit(f"用户不存在：{email}")
    return user.id


async def _serve() -> None:
    user_id = await _resolve_user_id()
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader()
    await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)

    while True:
        line = await reader.readline()
        if not line:  # EOF
            break
        raw = line.decode("utf-8").strip()
        if not raw:
            continue
        try:
            message = json.loads(raw)
        except json.JSONDecodeError:
            continue
        async with get_sessionmaker()() as session:
            resp = await handle_rpc(message, session=session, user_id=user_id)
        if resp is not None:
            sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            sys.stdout.flush()


def main() -> None:
    asyncio.run(_serve())


if __name__ == "__main__":
    main()
