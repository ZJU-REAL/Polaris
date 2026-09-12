"""插件管理代理（#754）：浏览器 → 后端（判权限）→ 内核宿主（干活）。

服务器形态下插件内核是独立的 Node 进程。它只认一个共享密钥、只挂在 compose
内部网络上，**不做用户认证**——认证在这里做，用的是既有的 owner 守卫。

为什么是代理而不是让前端直连内核：

- 认证只有一套。内核那侧再实现一遍 JWT 校验，等于两套认证栈，迟早在「谁算
  owner」上分叉，分叉的那一半就是漏洞。
- 在服务器上装插件 = 在服务端进程里跑第三方代码，对**所有账号**生效，不是
  桌面那种「装在自己机器上」。所以门槛是 owner，而不是「已登录」。
- 内核端口不对外，攻击面只剩后端这一个入口。

未配置内核（kernel_url / kernel_token 任一为空）时整族返回 503，让前端把
「这个部署没有插件能力」与「内核挂了」区分开。
"""

import json
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse

from app.core.config import get_settings
from app.models.user import User
from app.services.owner import require_owner

router = APIRouter(prefix="/plugins", tags=["plugins"])

# 安装要下载 + 解压，慢是正常的；读方法都是本地树操作，快。取一个够宽的上限。
_RPC_TIMEOUT = httpx.Timeout(connect=5.0, read=180.0, write=30.0, pool=5.0)


def _kernel_target() -> tuple[str, str]:
    settings = get_settings()
    url = (settings.kernel_url or "").rstrip("/")
    token = settings.kernel_token or ""
    if not url or not token:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, detail="KERNEL_NOT_CONFIGURED"
        )
    return url, token


@router.post("/rpc")
async def plugins_rpc(
    payload: dict[str, Any],
    _owner: User = Depends(require_owner),
) -> Any:
    """把一次 JSON-RPC 调用转给内核宿主。

    方法名与参数不在这里白名单化：内核自己有方法表（未知方法 404）与逐个参数的
    形状守卫，再抄一份清单只会多一处漂移点。这里只负责「谁能调」。
    """
    url, token = _kernel_target()
    try:
        async with httpx.AsyncClient(timeout=_RPC_TIMEOUT) as client:
            response = await client.post(
                f"{url}/rpc",
                json=payload,
                headers={"x-polaris-kernel-token": token},
            )
    except httpx.HTTPError as exc:
        # 连不上内核是运行故障，不是调用错误：502 让前端说「插件服务不可达」
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, detail=f"KERNEL_UNREACHABLE: {exc}"
        ) from exc

    body = response.json()
    if response.status_code >= 400:
        # 内核把方法失败表达成 4xx + {"error": ...}：原样透出错误文本，
        # 前端要靠 MarketError 的错误码分流展示（integrity-mismatch 等）
        raise HTTPException(response.status_code, detail=body.get("error", "kernel error"))
    return body.get("result")


@router.get("/events")
async def plugins_events(
    request: Request,
    _owner: User = Depends(require_owner),
) -> StreamingResponse:
    """把内核的 job 事件流（SSE）透传给浏览器。

    安装是长任务：四个阶段的进度经这条流回到前端。断流由客户端重连处理，
    这里不做缓冲——错过的进度不值得攒着，收尾的 job.done/job.error 会重发结论。
    """
    url, token = _kernel_target()

    async def relay():
        try:
            async with (
                httpx.AsyncClient(timeout=httpx.Timeout(None)) as client,
                client.stream(
                    "GET", f"{url}/events", headers={"x-polaris-kernel-token": token}
                ) as upstream,
            ):
                async for chunk in upstream.aiter_raw():
                    if await request.is_disconnected():
                        break
                    yield chunk
        except httpx.HTTPError as exc:
            # 流里报错只能用数据面：发一条结构化事件，别让前端以为只是断了
            yield f"data: {json.dumps({'type': 'kernel.error', 'message': str(exc)})}\n\n".encode()

    return StreamingResponse(
        relay(),
        media_type="text/event-stream",
        # nginx 默认会缓冲代理响应，SSE 必须关掉才逐条到达（与 /api 的其余流式面一致）
        headers={"cache-control": "no-cache", "x-accel-buffering": "no"},
    )
