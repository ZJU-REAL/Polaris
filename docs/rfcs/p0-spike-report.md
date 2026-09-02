# P0 Spike Report

| Field | Value |
| --- | --- |
| Status | In progress (gate review when all three spikes conclude) |
| Tracking issue | #564 |
| Gate rule | Any spike failing its acceptance bar → fall back to the Python-kernel variant; cost limited to the spikes |

## Spike 3 — JSON-RPC seam + cross-process config-tree reconciler ✅

Issue #574. Code: `spikes/p0/3-rpc-reconcile` (excluded from packaging; deletable wholesale).

Setup: `@polaris/kernel`'s `JsonRpcEndpoint` over a supervised `python3` child
process running a pure-stdlib sidecar (framing aligned with
`app/mcp/__main__.py`); a reconciler prototype that diffs a desired config
tree against the sidecar's reported live set, pushes `config.apply`, collects
per-component progress notifications into an effect log, and verifies
convergence via `config.report`. Crash model: the sidecar is stateless; on
death the supervisor respawns it empty and the reconciler re-applies the
desired tree (state lives kernel-side).

| Acceptance bar | Result |
| --- | --- |
| Bidirectional echo ×1000, RTT P95 < 5 ms | **P50 0.02 ms · P95 0.03 ms · P99 0.08 ms** (macOS, Python 3.14) |
| 5 MB single-line payload survives framing both directions | ✅ length + tail intact |
| Add / update / remove tree changes each trigger exactly the minimal component effect set | ✅ effect-log equality assertions (`start:src:pubmed` alone on add; `update:src:openalex` alone on config change; exactly two `stop`s on remove+disable) |
| kill -9 recovery < 5 s, reconverged, in-flight requests rejected deterministically | ✅ restart=1, report matches desired, in-flight call rejects with `sidecar exited` |

Findings for the real python-edge plugin (P1):

1. Line-delimited JSON-RPC over stdio is comfortably fast for engine-level
   seams (sub-0.1 ms RTT); no need for a heavier transport until proven
   otherwise.
2. 5 MB single-line payloads pass, but both ends buffer the full line —
   PDF-scale blobs should move via file paths or chunked notifications, not
   inline params. (Recorded as a python-edge design rule.)
3. "Sidecar stateless + kernel-side desired tree + respawn-then-reapply" is a
   sound crash model and keeps the reconciler trivial; the effect-minimality
   assertions carry over as the contract for the production reconciler.
4. `rejectAll` on process exit (inherited from the desktop supervisor design)
   is exactly right — no in-flight caller ever hangs across a crash.

## Spike 1 — kernel drives a full legacy-engine chain ⏳

Pending.

## Spike 2 — SQLite + sqlite-vec literature-store benchmark ⏳

Pending.
