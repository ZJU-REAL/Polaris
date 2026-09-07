#!/usr/bin/env python3
"""Polaris BYO runner agent —— tier-2 出站 WebSocket 参考实现（#695）。

跑在用户自己的机器上（NAT/内网也行）：只发**出站**连接回 Polaris 平台，
永不要求开入站端口。两个子命令：

    python agent.py register --server https://polaris.example --token prt_xxx
    python agent.py run

register 用一次性 token 换长期机器凭据（agent secret），存到
~/.polaris-runner/agent.json（0600，仅本用户可读）；run 建立出站 WS 长连接、
定期心跳、接收任务。参考实现级别：只会执行 echo 型任务（把 payload 原样回传），
真正的容器化实验执行由后续版本接入（docs/byo-runner.md）。

依赖：标准库 + websockets（pip install -r requirements.txt）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import socket
import sys
import urllib.error
import urllib.request
from pathlib import Path

CONFIG_PATH = Path.home() / ".polaris-runner" / "agent.json"
HEARTBEAT_INTERVAL = 30.0  # 服务端 90s 没帧判离线，30s 一跳留足余量
RECONNECT_DELAY = 5.0


def machine_info() -> dict:
    """注册时自述的机器信息（非敏感；进服务端 Resource.config.machine）。"""
    return {
        "hostname": socket.gethostname(),
        "os": platform.platform(),
        "python": platform.python_version(),
        "cpu_count": os.cpu_count(),
    }


def save_config(config: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    # 先建 0600 空文件再写入：避免默认 umask 下出现短暂的可读窗口
    fd = os.open(CONFIG_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(config, f, indent=2)
    print(f"credentials saved to {CONFIG_PATH} (0600)")


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        sys.exit(f"no config at {CONFIG_PATH}; run `python agent.py register` first")
    with open(CONFIG_PATH) as f:
        return json.load(f)


def cmd_register(args: argparse.Namespace) -> None:
    server = args.server.rstrip("/")
    body = json.dumps(
        {
            "token": args.token,
            "name": args.name or socket.gethostname(),
            "machine": machine_info(),
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        f"{server}/api/resources/runner-hosts/register",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req) as resp:
            data = json.load(resp)
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")
        sys.exit(f"registration failed: HTTP {e.code} {detail}")
    save_config(
        {
            "server": server,
            "resource_id": data["resource"]["id"],
            "secret": data["agent_secret"],
            "ws_path": data.get("ws_path", "/ws/runner-agents/connect"),
        }
    )
    print(f"registered as resource {data['resource']['id']} ({data['resource']['name']})")


def ws_url(config: dict) -> str:
    server = config["server"]
    scheme = "wss" if server.startswith("https") else "ws"
    host = server.split("://", 1)[1]
    return f"{scheme}://{host}{config['ws_path']}"


async def send_json(ws, frame: dict) -> None:
    await ws.send(json.dumps(frame, ensure_ascii=False))


async def heartbeat_loop(ws) -> None:
    while True:
        await asyncio.sleep(HEARTBEAT_INTERVAL)
        await send_json(ws, {"type": "heartbeat"})


async def handle_task(ws, frame: dict) -> None:
    """参考实现：echo 型任务原样回传，其余打印后回报 unsupported。

    真正的执行（docker 一次性容器 + 产物回传，ephemeral 承诺）由后续版本接入。
    """
    task_id = frame.get("task_id")
    payload = frame.get("payload") or {}
    print(f"[task] {task_id}: {json.dumps(payload, ensure_ascii=False)}")
    await send_json(
        ws, {"type": "event", "task_id": task_id, "data": {"status": "received"}}
    )
    if payload.get("kind") == "echo":
        await send_json(ws, {"type": "result", "task_id": task_id, "data": {"echo": payload}})
    else:
        await send_json(
            ws,
            {
                "type": "result",
                "task_id": task_id,
                "data": {"status": "unsupported", "detail": "reference agent only runs echo"},
            },
        )


async def run_once(config: dict) -> None:
    import websockets

    url = ws_url(config)
    async with websockets.connect(url) as ws:
        await send_json(
            ws,
            {"type": "auth", "resource_id": config["resource_id"], "secret": config["secret"]},
        )
        ready = json.loads(await ws.recv())
        if ready.get("type") != "ready":
            raise RuntimeError(f"unexpected first frame: {ready}")
        print(f"connected to {url} as resource {config['resource_id']}")
        hb = asyncio.create_task(heartbeat_loop(ws))
        try:
            async for raw in ws:
                try:
                    frame = json.loads(raw)
                except ValueError:
                    continue
                if frame.get("type") == "task":
                    await handle_task(ws, frame)
        finally:
            hb.cancel()


async def run_forever(config: dict) -> None:
    while True:
        try:
            await run_once(config)
            print("connection closed by server")
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 —— 常驻进程：断线/网络抖动一律重连
            print(f"connection error: {e!r}")
        print(f"reconnecting in {RECONNECT_DELAY:.0f}s ...")
        await asyncio.sleep(RECONNECT_DELAY)


def cmd_run(_args: argparse.Namespace) -> None:
    config = load_config()
    try:
        asyncio.run(run_forever(config))
    except KeyboardInterrupt:
        print("bye")


def main() -> None:
    parser = argparse.ArgumentParser(description="Polaris BYO runner agent (tier-2)")
    sub = parser.add_subparsers(dest="command", required=True)

    p_register = sub.add_parser("register", help="register this machine with a one-time token")
    p_register.add_argument("--server", required=True, help="Polaris server URL, e.g. https://polaris.example")
    p_register.add_argument("--token", required=True, help="one-time registration token (prt_...)")
    p_register.add_argument("--name", default=None, help="display name (default: hostname)")
    p_register.set_defaults(func=cmd_register)

    p_run = sub.add_parser("run", help="connect outbound and serve tasks")
    p_run.set_defaults(func=cmd_run)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
