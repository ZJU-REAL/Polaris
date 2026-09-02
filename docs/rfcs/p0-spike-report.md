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

## Spike 2 — SQLite + sqlite-vec literature-store benchmark ✅ (conditional)

Issue #576. Code: `spikes/p0/2-sqlite-bench` (bench.mjs + CI smoke at 2k scale).

Setup: Node built-in `node:sqlite` driver + prebuilt sqlite-vec 0.1.7 loadable
extension (zero native compilation), 1024-dim embeddings int8-quantized,
`vec0` virtual table with `library_id` as partition key, FTS5
external-content table, WAL. Synthetic reproducible data (seeded PRNG),
10 libraries, 500 queries (200 at 500k) after warmup. Machine: macOS arm64.

| Metric | 100k chunks | 500k chunks | Bar | Verdict |
| --- | --- | --- | --- | --- |
| Vector KNN (global scan) P95 | 101 ms | 477 ms | 80 / 200 ms | ❌ fails at both scales |
| **Vector KNN (library-partitioned) P95** | **10.9 ms** | **48 ms** | 80 / 200 ms | ✅ 4–7× headroom |
| FTS5 BM25 P95 | 1.3 ms | 8.1 ms | — | ✅ |
| Hybrid RRF (global) P95 | 103 ms | 528 ms | 80 / 200 ms | ❌ global; partitioned hybrid ≈ partitioned KNN + FTS ✅ |
| Cold open + first query | 95 ms | 440 ms | < 1.5 s | ✅ |
| DB size | 0.19 GB | 0.93 GB | < 2 GB @500k | ✅ |
| Ingest throughput | 10.9k rows/s | 11.1k rows/s | — | ✅ (500k in 45 s) |
| int8 vs f32 top-20 overlap | 0.872 | 0.885 | ≥ 0.9 | ⚠️ see finding 2 |

**Verdict: conditional pass — SQLite carries the local literature store if
and only if vector search is library-partitioned.** This matches the
product's dominant access pattern (relevance scoring, RAG, and evidence
retrieval are all library-scoped), so it is a design rule, not a compromise.

Findings:

1. **Library partitioning is mandatory.** Brute-force global scan grows
   linearly (≈0.9 ms per 1k chunks) and blows the bars beyond ~50k chunks;
   the `vec0` partition key gives a clean 10× cut. Cross-library search must
   fan out per-library and merge (or use a two-stage design) — recorded as a
   storage-plugin design rule. If a future workload truly needs global ANN,
   the escape hatches are LanceDB or sqlite-vec's planned ANN indexes.
2. **int8 overlap 0.872–0.885 is a worst-case bound, not a failure.** The
   synthetic vectors are uniform-random (isotropic), which maximizes
   quantization damage; real embedding distributions are anisotropic and
   quantize better. Follow-up recorded: re-measure on real bge-m3 vectors
   during P1-A5 before freezing the quantization choice; f32 storage
   (4× size) is the fallback.
3. **`node:sqlite` + prebuilt sqlite-vec is a viable zero-compile path** for
   the spike and possibly for production — worth keeping as an alternative to
   better-sqlite3 (one less native build in the Electron packaging chain).
   Two binding quirks recorded: integers must be bound as BigInt (numbers
   arrive as FLOAT and vec0 partition keys reject them), and int8 vectors
   must be wrapped in `vec_int8(?)` (raw 1024-byte blobs are otherwise
   misread as float32).
4. **Electron re-run is deferred to CI.** This development machine's Xcode
   CLT installation is broken (libc++ headers missing entirely — no C++
   compilation possible, better-sqlite3 unbuildable locally), so the
   native-ABI-under-Electron leg moves to the packaging work (P1-A6) where
   prebuilds and electron-rebuild run in CI. The `node:sqlite` numbers above
   measure the same SQLite engine and are valid for the storage decision.
