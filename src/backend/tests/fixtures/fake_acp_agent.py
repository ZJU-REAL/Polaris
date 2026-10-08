"""一个按脚本行事的 ACP agent，给测试当对端（#836）。

说的是真协议（按行 JSON-RPC over stdio），行为由提示词里的关键词决定：

- 默认：吐一段思考、一份计划、一个 read 工具调用（经 client 的 fs/read_text_file 读
  工作目录里的 notes.txt），最后回 "echo: <提示词>"。
- ``write``：先要 edit 权限，被允许才经 fs/write_text_file 写 out.txt。
- ``escape``：试图读 /etc/passwd，把 client 的回答（应当是错误）原样说出来。
- ``env``：报告自己有没有拿到 DATABASE_URL / SECRET_KEY / FAKE_AGENT_TOKEN。
- ``slow``：一直等，直到收到 session/cancel，回 stopReason=cancelled。
- ``crash``：直接退出进程。
- ``mcp``：把 session/new 收到的 mcpServers 说出来。

环境变量 FAKE_ACP_NO_LOAD=1 时不声明 loadSession；FAKE_ACP_NO_MCP=1 时不声明 HTTP MCP；
FAKE_ACP_IMAGE=1 时声明能看图。

当模型用（#840）：提示词里有「plain language model」那句时，吐一段思考，再回答
``llm[<模型>]: <请求正文>``（请求正文 = 最后一个分隔段之前那段），带图时追加
`` [images=N]``。会话开出来时报两个可选模型 fast / smart。
"""

import json
import os
import sys
import threading

_lock = threading.Lock()
_next_id = [1000]
_waiters: dict[int, dict] = {}
_events: dict[int, threading.Event] = {}
_cancelled: set[str] = set()
_sessions: dict[str, dict] = {}


def send(msg: dict) -> None:
    with _lock:
        sys.stdout.write(json.dumps(msg) + "\n")
        sys.stdout.flush()


def call(method: str, params: dict) -> dict:
    """向 client 发请求并阻塞等回应（读线程负责把回应塞进 _waiters）。"""
    with _lock:
        _next_id[0] += 1
        rid = _next_id[0]
    ev = threading.Event()
    _events[rid] = ev
    send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
    ev.wait(10)
    return _waiters.pop(rid, {"error": {"message": "no reply"}})


def update(sid: str, upd: dict) -> None:
    send(
        {"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": sid, "update": upd}}
    )


def text(t: str) -> dict:
    return {"type": "text", "text": t}


def handle_prompt(rid: int, params: dict) -> None:
    sid = params["sessionId"]
    prompt = "".join(b.get("text", "") for b in params.get("prompt", []))
    cwd = _sessions.get(sid, {}).get("cwd", os.getcwd())

    if "plain language model" in prompt:
        sections = prompt.split("\n\n---\n\n")
        body = sections[-2] if len(sections) >= 2 else prompt
        images = sum(1 for b in params.get("prompt", []) if b.get("type") == "image")
        model = _sessions.get(sid, {}).get("model", "default")
        update(sid, {"sessionUpdate": "agent_thought_chunk", "content": text("considering")})
        answer = f"llm[{model}]: {body}" + (f" [images={images}]" if images else "")
        for i in range(0, len(answer), 7):
            update(
                sid, {"sessionUpdate": "agent_message_chunk", "content": text(answer[i : i + 7])}
            )
        send({"jsonrpc": "2.0", "id": rid, "result": {"stopReason": "end_turn"}})
        return

    if "crash" in prompt:
        os._exit(3)

    if "slow" in prompt:
        update(sid, {"sessionUpdate": "agent_message_chunk", "content": text("working…")})
        for _ in range(200):
            if sid in _cancelled:
                send({"jsonrpc": "2.0", "id": rid, "result": {"stopReason": "cancelled"}})
                return
            threading.Event().wait(0.05)
        send({"jsonrpc": "2.0", "id": rid, "result": {"stopReason": "end_turn"}})
        return

    if "escape" in prompt:
        reply = call("fs/read_text_file", {"sessionId": sid, "path": "/etc/passwd"})
        update(sid, {"sessionUpdate": "agent_message_chunk", "content": text(json.dumps(reply))})
        send({"jsonrpc": "2.0", "id": rid, "result": {"stopReason": "end_turn"}})
        return

    if "env" in prompt:
        report = {k: (k in os.environ) for k in ("DATABASE_URL", "SECRET_KEY", "FAKE_AGENT_TOKEN")}
        update(sid, {"sessionUpdate": "agent_message_chunk", "content": text(json.dumps(report))})
        send({"jsonrpc": "2.0", "id": rid, "result": {"stopReason": "end_turn"}})
        return

    if "mcp" in prompt:
        servers = _sessions.get(sid, {}).get("mcpServers", [])
        update(sid, {"sessionUpdate": "agent_message_chunk", "content": text(json.dumps(servers))})
        send({"jsonrpc": "2.0", "id": rid, "result": {"stopReason": "end_turn"}})
        return

    if "write" in prompt:
        update(
            sid,
            {
                "sessionUpdate": "tool_call",
                "toolCallId": "w1",
                "title": "Write out.txt",
                "kind": "edit",
                "status": "pending",
                "rawInput": {"path": "out.txt"},
            },
        )
        perm = call(
            "session/request_permission",
            {
                "sessionId": sid,
                "toolCall": {"toolCallId": "w1", "title": "Write out.txt", "kind": "edit"},
                "options": [
                    {"optionId": "yes", "name": "Allow", "kind": "allow_once"},
                    {"optionId": "no", "name": "Reject", "kind": "reject_once"},
                ],
            },
        )
        outcome = (perm.get("result") or {}).get("outcome") or {}
        if outcome.get("optionId") == "yes":
            res = call(
                "fs/write_text_file",
                {
                    "sessionId": sid,
                    "path": os.path.join(cwd, "out.txt"),
                    "content": "written by agent",
                },
            )
            status = "failed" if "error" in res else "completed"
            update(
                sid,
                {
                    "sessionUpdate": "tool_call_update",
                    "toolCallId": "w1",
                    "status": status,
                    "content": [{"type": "content", "content": text("wrote out.txt")}],
                },
            )
            update(sid, {"sessionUpdate": "agent_message_chunk", "content": text("wrote it")})
        else:
            update(
                sid, {"sessionUpdate": "tool_call_update", "toolCallId": "w1", "status": "failed"}
            )
            update(sid, {"sessionUpdate": "agent_message_chunk", "content": text("not allowed")})
        send({"jsonrpc": "2.0", "id": rid, "result": {"stopReason": "end_turn"}})
        return

    update(sid, {"sessionUpdate": "agent_thought_chunk", "content": text("thinking about it")})
    update(
        sid,
        {
            "sessionUpdate": "plan",
            "entries": [
                {"content": "Read notes", "priority": "high", "status": "in_progress"},
                {"content": "Answer", "priority": "medium", "status": "pending"},
            ],
        },
    )
    update(
        sid,
        {
            "sessionUpdate": "tool_call",
            "toolCallId": "r1",
            "title": "Read notes.txt",
            "kind": "read",
            "status": "in_progress",
            "rawInput": {"path": "notes.txt"},
        },
    )
    res = call("fs/read_text_file", {"sessionId": sid, "path": os.path.join(cwd, "notes.txt")})
    content = (res.get("result") or {}).get("content", "") if "error" not in res else "missing"
    update(
        sid,
        {
            "sessionUpdate": "tool_call_update",
            "toolCallId": "r1",
            "status": "completed",
            "content": [{"type": "content", "content": text(content)}],
        },
    )
    for chunk in ("echo: ", prompt.split("---\n\n")[-1]):
        update(sid, {"sessionUpdate": "agent_message_chunk", "content": text(chunk)})
    send({"jsonrpc": "2.0", "id": rid, "result": {"stopReason": "end_turn"}})


def main() -> None:
    counter = [0]
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        msg = json.loads(raw)
        method, rid = msg.get("method"), msg.get("id")
        if method is None:
            _waiters[rid] = msg
            ev = _events.pop(rid, None)
            if ev:
                ev.set()
            continue
        params = msg.get("params") or {}
        if method == "initialize":
            send(
                {
                    "jsonrpc": "2.0",
                    "id": rid,
                    "result": {
                        "protocolVersion": 1,
                        "agentCapabilities": {
                            "loadSession": os.environ.get("FAKE_ACP_NO_LOAD") != "1",
                            "promptCapabilities": {
                                "image": os.environ.get("FAKE_ACP_IMAGE") == "1"
                            },
                            "mcpCapabilities": {
                                "http": os.environ.get("FAKE_ACP_NO_MCP") != "1",
                                "sse": False,
                            },
                        },
                        "authMethods": [],
                        "agentInfo": {
                            "name": "fake-agent",
                            "title": "Fake Agent",
                            "version": "0.1",
                        },
                    },
                }
            )
        elif method == "session/new":
            counter[0] += 1
            sid = f"sess-{os.getpid()}-{counter[0]}"
            _sessions[sid] = {"cwd": params.get("cwd"), "mcpServers": params.get("mcpServers", [])}
            models = {
                "currentModelId": "default",
                "availableModels": [
                    {"modelId": "fast", "name": "Fast"},
                    {"modelId": "smart", "name": "Smart"},
                ],
            }
            send({"jsonrpc": "2.0", "id": rid, "result": {"sessionId": sid, "models": models}})
        elif method == "session/load":
            sid = params["sessionId"]
            _sessions[sid] = {"cwd": params.get("cwd"), "mcpServers": params.get("mcpServers", [])}
            # 回放历史：client 不该把它当成这一轮的输出
            update(
                sid, {"sessionUpdate": "agent_message_chunk", "content": text("REPLAYED HISTORY")}
            )
            send({"jsonrpc": "2.0", "id": rid, "result": None})
        elif method == "session/set_model":
            _sessions.setdefault(params.get("sessionId", ""), {})["model"] = params.get("modelId")
            send({"jsonrpc": "2.0", "id": rid, "result": {}})
        elif method == "session/prompt":
            threading.Thread(target=handle_prompt, args=(rid, params), daemon=True).start()
        elif method == "session/cancel":
            _cancelled.add(params.get("sessionId", ""))
        elif rid is not None:
            send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "nope"}})


if __name__ == "__main__":
    main()
