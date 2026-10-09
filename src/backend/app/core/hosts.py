"""Host 头白名单（#850）：挡 DNS rebinding。

引擎只监听 127.0.0.1，但浏览器可以被骗着把请求发给它：攻击者让自己的域名先解析到
公网、再解析到 127.0.0.1，页面就能以「同源」身份读写本机引擎。这种请求的 Host 头
仍然是攻击者的域名——只认回环地址（加上 POLARIS_ALLOWED_HOSTS 里显式追加的名字）
就能把它挡在门外。

不用 Starlette 自带的 TrustedHostMiddleware：它按第一个冒号切端口，``[::1]:18080``
会被切成 ``[``，IPv6 回环永远不过。
"""

from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Receive, Scope, Send


def host_without_port(raw: str) -> str:
    """``Host`` 头去端口、去 IPv6 方括号、转小写。"""
    host = raw.strip().lower()
    if host.startswith("["):
        end = host.find("]")
        return host[1:end] if end != -1 else host
    if host.count(":") == 1:  # name:port；多个冒号是没加括号的裸 IPv6，原样比较
        host = host.split(":", 1)[0]
    return host


class AllowedHostMiddleware:
    def __init__(self, app: ASGIApp, allowed: list[str]) -> None:
        self.app = app
        self.allow_any = "*" in allowed
        self.allowed = frozenset(allowed)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if self.allow_any or scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        raw = ""
        for name, value in scope.get("headers") or []:
            if name == b"host":
                raw = value.decode("latin-1")
                break
        if host_without_port(raw) in self.allowed:
            await self.app(scope, receive, send)
            return
        if scope["type"] == "websocket":
            # 握手还没被接受：直接 close 即拒绝（ASGI 规范里等同于 403）
            await send({"type": "websocket.close", "code": 1008})
            return
        response = PlainTextResponse("Invalid host header", status_code=400)
        await response(scope, receive, send)
