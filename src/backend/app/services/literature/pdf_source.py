"""按用户给的公开链接取 PDF。

这条路和别处不同：**URL 由用户输入**，所以它是一个我们代替用户发起请求的入口，
天然带 SSRF 面。下面的防护按「攻击者完全控制那个域名」来设计，而不是按「用户只是
粘错了链接」。
"""

import asyncio
import ipaddress
import logging
import socket
from urllib.parse import urljoin, urlsplit

import httpx

from app.core.config import get_settings

logger = logging.getLogger(__name__)

#: 和上传走同一个上限，免得两条入口对同一篇论文给出不同答案。
MAX_PDF_BYTES = 100 * 1024 * 1024
MAX_REDIRECTS = 5
_CONNECT_TIMEOUT = 15.0
_READ_TIMEOUT = 60.0


class PdfUrlError(RuntimeError):
    """这个链接不能用（不合法、指向内网、下载失败或者拿回来的不是 PDF）。"""


def _check_shape(parsed: object) -> None:
    scheme = getattr(parsed, "scheme", "")
    if scheme not in ("http", "https") or not getattr(parsed, "hostname", None):
        raise PdfUrlError("只支持公开的 http/https 链接")
    if getattr(parsed, "username", None) or getattr(parsed, "password", None):
        raise PdfUrlError("链接里不能带用户名或密码")


def _reject_non_public(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> None:
    """挡掉本机 / 内网 / 保留地址。

    用 ``is_global`` 而不是自己列网段：私有、环回、链路本地、保留段、
    运营商级 NAT（100.64/10）它都算在内，少写一条就是一个洞。
    """
    if not address.is_global:
        raise PdfUrlError("链接不能指向本机、内网或保留地址")


async def _precheck_dns(parsed: object) -> None:
    """连接之前先看这个主机名解析到哪里。"""
    hostname = str(getattr(parsed, "hostname", "") or "")
    try:
        literal = ipaddress.ip_address(hostname)
    except ValueError:
        pass
    else:
        _reject_non_public(literal)
        return

    scheme = getattr(parsed, "scheme", "http")
    port = getattr(parsed, "port", None) or (443 if scheme == "https" else 80)
    try:
        rows = await asyncio.get_running_loop().getaddrinfo(
            hostname, port, type=socket.SOCK_STREAM
        )
    except OSError as e:
        raise PdfUrlError(f"解析不了这个地址：{hostname}") from e
    addresses = {ipaddress.ip_address(row[4][0]) for row in rows}
    if not addresses:
        raise PdfUrlError(f"解析不了这个地址：{hostname}")
    for address in addresses:
        _reject_non_public(address)


def _check_connected_peer(response: httpx.Response) -> None:
    """连上之后，再看**真正连到了哪儿**。

    只做连接前的 DNS 校验是挡不住的：校验查一次、httpx 连接时又查一次，中间那一瞬
    足够攻击者的 DNS 换个答案——第一次给公网 IP 过关，第二次给 127.0.0.1。这是
    教科书式的 DNS 重绑定，而链接正是攻击者提供的，所以这不是理论风险。

    这里在读响应体**之前**核对实际对端地址，命中就断开。请求头已经发出去了，挡不住
    那一次「盲发」，但拿不到任何内网响应内容。
    """
    stream = response.extensions.get("network_stream")
    if stream is None:  # 测试替身 / 非默认 transport：没有可核对的对端
        return
    peer = stream.get_extra_info("server_addr")
    if not peer:
        return
    try:
        address = ipaddress.ip_address(str(peer[0]))
    except ValueError:
        return
    _reject_non_public(address)


async def download_pdf(url: str) -> bytes:
    """按公开链接取回 PDF 字节。

    重定向自己走，**每一跳都重新校验**：只验第一个 URL 是最常见的漏法，
    一个公网地址 302 到 ``http://169.254.169.254/`` 就绕过去了。
    """
    settings = get_settings()
    proxy = settings.outbound_proxy or None
    current = url.strip()

    async with httpx.AsyncClient(
        proxy=proxy,
        timeout=httpx.Timeout(_READ_TIMEOUT, connect=_CONNECT_TIMEOUT),
        follow_redirects=False,
        headers={"User-Agent": "Polaris/1.0 PDF fetcher"},
    ) as client:
        for hop in range(MAX_REDIRECTS + 1):
            parsed = urlsplit(current)
            _check_shape(parsed)
            await _precheck_dns(parsed)
            try:
                async with client.stream("GET", current) as response:
                    if response.is_redirect:
                        location = response.headers.get("location")
                        if not location:
                            raise PdfUrlError("对方返回了没有目标的重定向")
                        if hop >= MAX_REDIRECTS:
                            raise PdfUrlError("重定向次数过多")
                        current = urljoin(current, location)
                        continue
                    # 走代理时对端是代理本身（往往就是个内网地址），核对它没有意义；
                    # 这种部署下「请求到底落在哪」由代理决定，不在我们这一层。
                    if proxy is None:
                        _check_connected_peer(response)
                    response.raise_for_status()
                    chunks: list[bytes] = []
                    total = 0
                    async for chunk in response.aiter_bytes():
                        total += len(chunk)
                        if total > MAX_PDF_BYTES:
                            raise PdfUrlError("PDF 超过 100 MB 上限")
                        chunks.append(chunk)
            except PdfUrlError:
                raise
            except httpx.HTTPError as e:
                raise PdfUrlError(f"下载失败：{type(e).__name__}: {e}") from e
            content = b"".join(chunks)
            if not content:
                raise PdfUrlError("下载到的内容是空的")
            return content
    raise PdfUrlError("重定向次数过多")
