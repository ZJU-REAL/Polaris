# P3 Implementation Plan: Autonomous Experiments

| | |
| --- | --- |
| Status | Accepted |
| Date | 2026-09-07 |
| Tracking | (tracking issue, see repository) |
| Design | [Polaris 2.0 design report](2026-09-02-polaris-2.0-design-report.zh.md) §8.3, §13–15, §23 |

P2/P2.5 delivered autonomous discovery on the literature side. P3 generalizes the
execution side: the ML-specific runner becomes one backend among many behind the
Runner v2 contract, research processes become data (process packs), and the first
non-CS backends land. The design report's product exit (a non-CS pilot group running
real experiments) requires real users; the engineering exit below is what this plan
delivers.

## Engineering exit criteria

- Runner v2 contract in place; the existing ML runner runs unchanged as the
  `python-ml` backend behind it (dispatch by `plan.backend`, default preserved).
- An `openfoam` or `ngspice` run works end to end in a container: materials
  validated against the manifest contract, fixed-template launch, result files
  collected and parsed into metrics. An `fmu` run works via FMPy on a fixture FMU.
- One built-in process pack drives an experiment run through the Navigator
  (pack loaded → plan generated), with free-form planning preserved as the
  empty-pack fallback.
- Resources and leases exist: an exclusive resource cannot be double-leased; a
  second run queues in `prepare` instead of failing.

## Work breakdown

- **R1 Runner v2 contract + registry + python-ml adapter.** `RunnerPlugin`
  protocol (validate/prepare/launch→str handle/poll/collect→ResultBundle/cancel/
  cleanup), `RunnerManifest` (backend id, interaction mode, side-effect class,
  materials contract, io schema, resource needs, license needs, credential kinds,
  prompt-pack ref). The existing 19-primitive runner wraps as `python-ml` — its
  requirements.txt/run.sh contract demotes from global validation to that
  backend's manifest. Dispatch by explicit `plan.backend` with the default kept.
  The safety invariant is restated in the contract: LLMs produce file contents
  only; the platform runs fixed template commands with manifest-whitelisted args.
- **R2 resources, leases, credentials.** `Resource` (kind host/queue/license-pool/
  instrument, capacity, exclusivity, encrypted credential ref) and `ResourceLease`
  (run↔resource, exclusivity enforced, queueing in prepare). `SSHCredential`
  generalizes to polymorphic `ConnectionCredential` (ssh/grpc/visa/http) on the
  existing Fernet layer, with a compatibility view for current callers.
- **R3 process packs, phase 1.** The `research-process` YAML format (phases,
  actions, checks, rubrics, loop topology, guidance; gates/irreversible fields
  in schema but inert per the deferral decision). Materialize the existing
  experiment plan function's output as the built-in `base/experiment` pack; the
  Navigator loads the selected pack and generates the plan from it; free-form
  planning = empty pack + full guidance. Pack storage on disk (file-over-app),
  validated on load; user editing UI deferred.
- **R4 zero-license container backends.** `openfoam` and `ngspice` RunnerPlugins:
  container launch from fixed templates, materials contracts (case directory /
  netlist), result-file channel (CSV/log parsers → summary metrics; NaN flagged,
  not dropped). CI-testable with the ngspice container (small); openfoam smoke
  gated on image availability.
- **R5 fmu backend.** FMPy-based runner consuming any FMI 2.0/3.0 FMU — the
  highest-leverage integration (200+ tools export FMUs). Fixture FMU in tests.
- **R6 BYO runner, tier 1.** Formalize the SSH path: runner agent version pinned
  and auto-pushed on first connect, ephemeral container execution by default.
  Tier 2 (outbound-WebSocket agent for NAT'd machines) is deferred with the
  protocol sketched in the pack docs.

Deferred beyond this plan: session backends with license queueing (pyansys/comsol),
`visa-instrument` physical backends, commercial EDA, tier-2 BYO transport, pack
sharing/versioning marketplace.

## Sequencing

Wave 1: R1. Wave 2: R2 · R3 (parallel; R3 touches the navigator, R2 the schema).
Wave 3: R4 · R5 (parallel, both on R1+R2). Wave 4: R6.

Same per-PR discipline as P1–P2.5.
