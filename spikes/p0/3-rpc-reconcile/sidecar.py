"""P0 Spike 3: minimal Python sidecar for the Node<->Python engine seam.

Line-delimited JSON-RPC 2.0 over stdio, framing aligned with
src/backend/app/mcp/__main__.py (one JSON object per line). Pure stdlib.

State model: the sidecar holds a flat "live set" of config entries, mimicking
a python-edge process that hosts components. `config.apply` reconciles the
live set toward the pushed desired entries and reports each individual
component action as a progress notification, so the Node reconciler can
assert the minimal effect set. The process itself is stateless across
restarts: after a crash the supervisor respawns it empty and the reconciler
re-applies the desired tree (state lives in the kernel, not here).
"""

from __future__ import annotations

import json
import sys
from typing import Any

live: dict[str, dict[str, Any]] = {}


def send(msg: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def notify(method: str, params: Any) -> None:
    send({"jsonrpc": "2.0", "method": method, "params": params})


def handle(method: str, params: Any) -> Any:
    if method == "initialize":
        return {"name": "spike-sidecar", "pid": __import__("os").getpid()}
    if method == "echo":
        return params
    if method == "blob.echo":
        # Large-payload probe: return the byte length plus a checksum-ish tail.
        blob = params.get("blob", "") if isinstance(params, dict) else ""
        return {"length": len(blob), "tail": blob[-16:]}
    if method == "pdf.parse":
        return {"pages": 0, "note": "stub"}
    if method == "config.report":
        return {"entries": sorted(live.values(), key=lambda e: e["id"])}
    if method == "config.apply":
        desired = {e["id"]: e for e in params.get("entries", [])}
        actions: list[dict[str, str]] = []
        for eid in sorted(set(live) - set(desired)):
            del live[eid]
            actions.append({"op": "stop", "id": eid})
        for eid, entry in sorted(desired.items()):
            if eid not in live:
                live[eid] = entry
                actions.append({"op": "start", "id": eid})
            elif live[eid] != entry:
                live[eid] = entry
                actions.append({"op": "update", "id": eid})
        for action in actions:
            notify("config.progress", action)
        return {"actions": actions}
    raise ValueError(f"method not found: {method}")


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        mid = msg.get("id")
        method = msg.get("method")
        if not isinstance(method, str):
            continue
        try:
            result = handle(method, msg.get("params"))
            if mid is not None:
                send({"jsonrpc": "2.0", "id": mid, "result": result})
        except Exception as exc:  # noqa: BLE001 - single failure boundary
            if mid is not None:
                send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32000, "message": str(exc)}})


if __name__ == "__main__":
    main()
