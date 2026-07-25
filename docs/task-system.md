# The Task System

Everything in Polaris that takes minutes to days — building a literature library, generating ideas,
running an experiment on a GPU box, drafting a paper — runs as a **task**: a persisted, resumable,
auditable agent run. The UI calls these "任务 / Tasks". In the code they are called *voyages*
(`VoyageRun`, `VoyageStep`, `run_voyage`, `/voyages/...`); this document uses "task" and "run" in
prose and keeps the code names when naming actual identifiers.

This is the implementation-level document. For the one-page conceptual view (Navigator / Helm /
Sextant), see [Core Concepts](concepts.md#the-voyage-long-running-agent).

- Data model: `src/backend/app/models/voyage.py`
- Engine and agents: `src/backend/app/agents/voyage/`
- Business logic and visibility: `src/backend/app/services/voyages.py`
- HTTP API: `src/backend/app/api/voyages.py` (mounted under `/api`)
- Worker and schedules: `src/backend/worker/tasks.py`, `src/backend/worker/settings.py`
- UI: `src/frontend/src/features/voyages/`, plus the Tasks tabs in the topic and lab workspaces

---

## 1. What a task is

### 1.1 The three tables

Everything about a run lives in three tables, all defined in `app/models/voyage.py`.

#### `voyage_runs` — `VoyageRun`

One row per task.

| Column | What it holds |
| --- | --- |
| `id` | UUID primary key; the id in the URL `/voyages/{id}` and in the SSE channel name. |
| `kind` | What sort of task this is (`wiki_ingest`, `experiment`, `daily_feed_sync`, …). Everything else — run mode, task level, starting plan, done criteria — is derived from it. See [§1.3](#13-the-kind-catalogue). |
| `mode` | `pipeline` \| `template` \| `loop`. Not chosen by the user or the LLM: the engine recomputes it from `kind` via `mode_for_kind()` on the first drive and overwrites whatever is stored (this is also how runs created before the field existed get fixed up). |
| `goal` | Human-readable one-liner shown in the list ("文献调研增量更新：<library>"). Also fed to the LLM as `{goal}` in prompt templates. |
| `status` | Run state, see [§1.2](#12-the-run-state-machine). Indexed — the worker's startup reconcile scans it. |
| `plan` | JSON snapshot of the current step list. **Derived, not authoritative**: the real plan is the `voyage_steps` rows, and `_regen_plan_snapshot()` rebuilds this from them after every plan change. It exists so the API and the progress bar have something cheap to read. |
| `cursor` | Index of the current step within the active (non-obsolete) step list. Rewritten on every loop iteration; used for the "step 3 of 7" display. |
| `plan_iteration` | Incremented every time the plan is edited (replanning, signal-driven edit, budget cut). Each step records which iteration created it. |
| `done_criteria` | Optional `{checks: [...]}` evaluated once all steps are finished. `None` means "all steps passed ⇒ done". Filled at planning time from `done_criteria_for_kind()`; today only `experiment` has one. |
| `checkpoint` | The run's scratch workspace, and the reason resume works. See [§4.4](#44-checkpoints). |
| `budget` | `{max_tokens: int \| None, ...}`. See [§4.5](#45-budgets). |
| `usage` | Running token totals `{prompt_tokens, completion_tokens, total_tokens}`. |
| `project_id` | Owning topic, nullable. `ON DELETE CASCADE`. |
| `library_id` | Owning literature library, nullable. `ON DELETE SET NULL`. |
| `created_by` | User who started it; `NULL` for cron-created runs. `ON DELETE SET NULL`. |

`project_id` and `library_id` are the two scope columns, and **both can be null at once**. That
combination means "platform-level task" — today only `daily_feed_sync`, which belongs to the whole
lab. This matters for permissions ([§3](#3-who-can-see-and-do-what)) and for the UI grouping.

#### `voyage_steps` — `VoyageStep`

One row per step. Steps are never deleted; superseded ones are marked `obsolete` so the history stays
intact.

| Column | What it holds |
| --- | --- |
| `run_id` | Owning run, `ON DELETE CASCADE`. |
| `seq` | Creation order. Immutable once written, unique per run — the stable anchor for audit and for references from plan edits. |
| `rank` | Display order = execution order. Gap-numbered (0, 100, 200, …) so an inserted step can take a value in between without renumbering anything. Scheduling rule: sort by `(rank, seq)`, take the first step that is not `passed`. |
| `title` | What the step is called in the UI. |
| `action` | Key into the action registry (`app/agents/voyage/actions.py`). |
| `params` | Arguments for the action. The engine also injects a `diagnosis` key here when it retries a failed step. |
| `acceptance` | `{text: <human-readable criterion> \| None, checks: [...] \| None}`. See [§4.3](#43-acceptance-checks). |
| `requires_gate` | If set, the name of a human-approval type (`compute_budget`, `idea_goal`, `idea_pivot`, …). The step will not run until someone approves. |
| `budget` | Per-step limits, currently `{max_attempts}` (`max_tokens` / `max_gpu_hours` are accepted by the schema but not enforced by the engine). |
| `observation` | Whatever the action returned. A key named `error` in here means "the step blew up", which is treated differently from "the step ran but did not pass". |
| `verdict` | `{passed: bool, reason: str}` from Sextant. |
| `status` | `pending` → `running` → `passed` / `failed`, plus `obsolete`. (`verifying` is listed in the model comment but the engine never writes it to a step — verification shows up on the *run* status instead.) |
| `attempt` | Current attempt number. Reset to 0 by resume. |
| `attempts` | Full archive of every attempt: `[{attempt, observation, verdict, tokens, started_at, finished_at}]`. Streamed events are not persisted, so anything needed for audit is written here. |
| `provenance` | `{plan_iteration, on_failure?, wrapup?}` — which plan iteration created this step, and its failure/wrap-up flags. |
| `tokens` | Tokens spent by this step (action + verification). |
| `started_at` / `finished_at` | Timing, used for the duration column. |

#### `voyage_terminal_logs` — `VoyageTerminalLog`

The task detail page has a terminal view. It is fed live over SSE, but SSE has no history, so the two
kinds of output worth keeping are also written here (`app/services/voyage_logs.py`):

| Column | What it holds |
| --- | --- |
| `id` | Auto-increment integer — this *is* the ordering. Read back ascending to reconstruct the terminal. |
| `run_id` | Owning run, `ON DELETE CASCADE`. |
| `event` | `log` (a structured progress line) or `llm` (one complete model output). |
| `level` | For `log` rows: `info` / `step` / `success` / `error` / `plan` / `budget` / `gate` — the frontend colours by this. |
| `stage` | For `llm` rows: which routing stage produced it (`navigator`, `librarian`, …). |
| `message` | The log line, or the full model output. Truncated at 50 000 chars. |
| `at` | Timestamp. |

What is **not** stored: the high-frequency `llm_delta` token increments, and the `status` / `step`
events. Those exist only on the live stream. Rows older than 30 days are deleted opportunistically
(at most once every 10 minutes, piggy-backed on a write), and reads return at most the newest 3 000
rows. Writing a log line is best-effort — a failure is logged as a warning and never affects the run.

### 1.2 The run state machine

```text
planning ──> executing ──> verifying ──┬──> executing        (next step)
                  ^                    ├──> executing        (retry the same step, with diagnosis)
                  │                    ├──> replanning ──> executing
                  │                    ├──> paused_gate      (waiting for a human)
                  ├────────────────────┼──> paused_error     (stopped, fixable, retryable)
                                       └──> done / failed
```

`done`, `failed` and `cancelled` are terminal (`TERMINAL_STATUSES`). `cancelled` can be written from
outside at any time; see [§3.3](#33-cancel-and-retry).

### 1.3 The kind catalogue

`kind` is the single knob. It decides the run mode, the task level, the starting plan, and (for
`experiment`) the done criteria.

| kind | Mode | Level | Starting plan | Created by |
| --- | --- | --- | --- | --- |
| `wiki_bootstrap` | pipeline | library | `wiki_plan()` (7 steps) | `POST /projects/{id}/ingest` or `POST /libraries/{id}/ingest/run` with `mode=bootstrap` |
| `wiki_ingest` | pipeline | library | `wiki_plan()` (7 steps) | same two endpoints with `mode=incremental`; also the 03:00 UTC cron |
| `daily_feed_sync` | pipeline | library (platform-level) | `daily_feed_plan()` (4 steps) | the 01:30 UTC cron, or `POST /daily/refresh` (admin only) |
| `idea_forge` | pipeline | topic | `forge_plan()` (7 steps) | `POST /projects/{id}/forge` |
| `idea_review` | pipeline | topic | `review_plan()` (2 steps, expands at runtime) | `POST /projects/{id}/review/tournament` |
| `idea_proposal` | template | topic | `proposal_plan()` (8–9 steps) | `POST /projects/{id}/ideas/deep` |
| `experiment` | loop | topic | `experiment_plan()` (3 fixed + round 1) | `POST /projects/{id}/experiments` |
| `paper_writing` | pipeline | topic | `writing_plan()` (one step per section + compiles) | `POST /manuscripts/{id}/draft` |
| `paper_review` | pipeline | topic | `paper_review_plan()` (6 steps) | `POST /manuscripts/{id}/review` |
| `presentation` | pipeline | topic | `presentation_plan()` (4 steps) | `POST /projects/{id}/presentations` |
| `custom` | loop | topic | the workflow skill's own steps, written into `run.plan` at creation | `POST /skills/{id}/run` |
| `demo` | loop | topic | `demo_plan()` (3 steps, one behind a `compute_budget` approval) | `POST /voyages` |

Two things worth knowing:

- **`POST /voyages` only accepts `kind: "demo"`.** `VoyageKind` in `app/schemas/voyage.py` is
  `Literal["demo"]`. Every real task type is created by its own domain endpoint, which builds the
  `VoyageRun` row itself and then enqueues `run_voyage`. There is no generic "start any task"
  endpoint.
- **No kind currently uses free-form LLM planning.** `Navigator.plan()` has an explicit branch with
  a fixed plan template for every kind in the table above; `custom` never reaches Navigator at all
  because `run.plan` is already set. The LLM-writes-the-plan path (`PLAN_SYSTEM_PROMPT`, workflow
  skill suggestion) is live code and would fire for a new kind with no template, but nothing hits it
  today. "Run mode" is about *failure handling*, not about who wrote the plan.

### 1.4 Run modes

`mode_for_kind(kind)` is a three-line function:

```python
PIPELINE_KINDS = {"wiki_bootstrap", "wiki_ingest", "daily_feed_sync", "idea_forge",
                  "idea_review", "paper_writing", "paper_review", "presentation"}
TEMPLATE_KINDS = {"idea_proposal"}
# everything else -> "loop"
```

What actually differs in the engine:

| | pipeline | template | loop |
| --- | --- | --- | --- |
| Default retries on a crash (`observation.error`) | 1 attempt — no implicit retry | 2 attempts | 2 attempts |
| A step that ran but did not meet its acceptance criteria | stop; no replanning | deterministic branch table (`proposal_replan`), LLM fallback | LLM produces an incremental plan edit (`Navigator.on_result`) |
| Where it stops | `paused_error` (or `failed` if the step declares `on_failure: "fail"`) | `paused_error` after 2 replans | `paused_error` after 2 plan edits, or if the model returns "finish"/nothing |
| Plan can grow while running | only via the runtime signal table (see below) | yes | yes |

Notes:

- The retry defaults come from `VoyageEngine._max_attempts()`. A step can override with
  `budget.max_attempts`; pipeline steps that are safe to retry are expected to say so explicitly,
  because pipeline steps usually have side effects.
- **Crash vs. judgement is the key distinction.** If `observation.error` is set, the step is retried
  in place with the failure reason injected into `params["diagnosis"]`. If the step ran fine but the
  acceptance check said no, retrying the identical step is pointless, so it goes straight to the
  mode-specific branch.
- Gate and cancel semantics are identical in all three modes.
- `pipeline` does not mean "the plan is frozen". `idea_review` is a pipeline kind whose
  `review.pair` step emits a `plan_signal`, and the engine expands it into N debate steps through a
  **deterministic branch table** (`SIGNAL_TABLES` in `plan_edit.py`, keyed by kind — currently
  `experiment` and `idea_review`). That path never calls the LLM, which is why it is allowed in
  pipeline mode. The branch tables are idempotent, so a resume that replays the same signal does not
  duplicate steps.

---

## 2. Task levels: library tasks vs. topic tasks

Tasks are split into two levels, and the split is decided **by `kind`**, via
`LIBRARY_KINDS = {"wiki_bootstrap", "wiki_ingest", "daily_feed_sync"}`.

- **Library tasks** — building a library, incrementally updating it, and the daily new-paper fetch.
  These belong to the literature library itself, not to any topic. A topic merely links a library in
  order to use its corpus. They show up in the **lab workspace**, and are filtered *out* of a topic's
  task list.
- **Topic tasks** — everything else: ideas, proposals, experiments, drafting, review, slides,
  workflow skills, demo.

**Why by `kind` and not by `library_id`?** Because `library_id` was added later. Runs created before
libraries became first-class objects only carry a `project_id` and have no `library_id` at all. If
the filter keyed on `library_id`, those legacy ingest runs would be classified as topic tasks and
leak into topic task lists. `kind` is stable across that migration, so it is the safe discriminator.
`list_voyages()` says exactly this, and the frontend mirrors the same list as
`LIBRARY_TASK_KINDS` in `src/frontend/src/features/voyages/VoyagesPage.tsx`.

`daily_feed_sync` is the extreme case: it is a library-level kind that has *no* library either. Both
scope columns are null because the daily feed is shared by the whole lab.

---

## 3. Who can see and do what

### 3.1 The list (`_visible_filter`)

`list_voyages()` applies `_visible_filter()` from `app/services/voyages.py`, which is an `OR` of four
clauses:

1. `project_id` is one of the topics I am a member of (`project_members`);
2. `library_id` is one of the libraries linked by a topic I am a member of
   (`topic_source_libraries`);
3. `library_id` is one of the libraries I curate (`direction_library_curators`);
4. I am a platform admin (`users.role == "admin"`) — then everything is visible.

Clauses 2 and 3 exist because a library task's `project_id` is deliberately left empty
(`create_ingest_voyage` sets only `library_id`, so library tasks do not pollute topic task lists).
Filtering by topic membership alone would hide them entirely, and standalone libraries with no origin
topic are the normal case.

Then, if `project_id` is passed as a query parameter, the list is additionally narrowed to that
topic's own tasks **and library kinds are excluded** — those belong in the lab workspace.

Note what this means for a platform-level task: `daily_feed_sync` runs have both scope ids null, so
clauses 1–3 can never match. Only clause 4 does. **Non-admins do not see the daily fetch task at
all.**

### 3.2 The detail (`get_voyage`)

Detail, logs, SSE, cancel and retry all go through `get_voyage()`, which is stricter and checks three
whitelists in order. No access is reported as `404 VOYAGE_NOT_FOUND`, not `403` — existence is not
leaked.

1. **Topic-scoped run** (`project_id` set): the caller must be a member of that topic.
2. **Library-scoped run** (`library_id` set): the caller must pass `can_manage_library()` — i.e. be a
   platform admin, the creator, or a curator of that library. Note this is a *write*-level check, and
   it is stricter than the list filter: being a member of a topic that merely links the library gets
   you the row in the list, but not the detail page.
3. **Platform-level run** (both scope ids null): platform admins only. Without this branch even an
   admin would get a 404 on the daily fetch task, taking its logs, SSE stream, cancel and retry down
   with it. The rule matches the rest of the daily feed's admin surface (managing subscribed
   categories, manual refresh, the embedding toggle).

The library branch needs the full `User` object (for role and curator lookup), so the API layer
always passes `user=`. Call sites that only have a `user_id` degrade to topic-membership plus a
role lookup.

### 3.3 Cancel and retry

Both live in `app/api/voyages.py` and are open to anyone who can reach the task detail — there is no
extra role check beyond §3.2.

- **Cancel** — `POST /voyages/{id}/cancel`. Cooperative: it just writes `status = "cancelled"`.
  `409 VOYAGE_ALREADY_FINISHED` if the run is already terminal. A running engine notices at the next
  step boundary: `_loop()` re-reads the status from the database before every step, and every status
  write goes through a conditional `UPDATE ... WHERE status != 'cancelled'` so an in-flight engine can
  never resurrect a cancelled run.
- **Retry** — `POST /voyages/{id}/resume`. Only valid from `paused_error`
  (`409 VOYAGE_NOT_PAUSED_ERROR` otherwise). It flips the status to `executing` and enqueues
  `resume_voyage`. The engine's `resume()` resets every `failed` or `running` step back to `pending`
  with `attempt = 0` (history is already archived in `attempts`), then drives from there. Work that
  already passed is not redone — this is the "fix the bug, pick up where it stopped" path.
- **Approval** — approving a gate in `POST /gates/{id}/approve` also enqueues `resume_voyage`.
  Rejecting it sets the run to `failed`.

### 3.4 Where tasks appear in the UI

Both workspaces render the same list component (`VoyagesList` / `TasksTab`), differing in scope and
in which kinds the type filter offers.

- **Topic workspace** (`/t/:id?tab=tasks`, `DashboardPage.tsx`) — calls `GET /voyages?project_id=…`,
  so it shows that topic's own tasks only, with library kinds already filtered out server-side. The
  type dropdown is limited to `TOPIC_TASK_KINDS` (all kinds minus the library ones), because
  offering library kinds here would only produce filters that always come back empty.
- **Lab workspace** (`/lab`, Tasks tab, `LabPage.tsx`) — calls `GET /voyages` with no `project_id`,
  so it gets everything visible to the caller, including runs with no topic. Rows are grouped by
  ownership: one group per topic, one group per library ("library tasks, outside topics"), and a
  final "other tasks" group for runs with neither — which is where the daily fetch lands. The type
  dropdown splits into two option groups, library kinds and topic kinds.

Both lists poll every 30 s and offer the same status filters: all / active / waiting / done /
failed-or-cancelled.

---

## 4. How it works

### 4.1 The execution chain

```text
domain endpoint or cron
   │  builds the VoyageRun row (kind, goal, checkpoint["params"], budget, scope ids)
   │  enqueues ARQ job "run_voyage" with the run id
   ▼
worker: run_voyage  ──>  VoyageEngine.run(run_id)  ──>  _drive()
   │
   ├─ align run.mode with mode_for_kind(kind)
   ├─ snapshot the topic's enabled skills into checkpoint["skills"] (once per run)
   ├─ if run.plan is None: Navigator.plan()  ──>  status "planning"
   ├─ materialise a voyage_steps row per plan entry (_ensure_step_rows)
   └─ _loop():
        ├─ cancelled in the DB? stop.
        ├─ pick the first non-passed step by (rank, seq); none left -> _finalize()
        ├─ budget exhausted? -> wrap-up / obsolete the rest / paused_error
        ├─ step needs approval and has none? -> create a Gate, status paused_gate, stop
        ├─ Helm.execute()      -> observation
        ├─ Sextant.verify()    -> verdict {passed, reason}
        ├─ passed -> apply any plan_signal edits, continue
        └─ failed -> _handle_failure() (retry / replan / plan-edit / stop)
```

The three agents:

- **Navigator** (`navigator.py`) — planning. Produces the starting step list (a fixed template per
  kind today), and on failure either replans the tail (`replan()`, template mode) or emits an
  incremental plan edit (`on_result()`, loop mode). LLM output is parsed leniently (first `{` to last
  `}`) and then validated strictly; three bad responses in a row raise `NavigatorError`, which the
  engine turns into `failed` (during planning) or `paused_error` (during replanning).
- **Helm** (`helm.py`) — execution, and it is deliberately tiny: look up `action` in the registry,
  call it, and **never let an exception escape**. An unknown action or any raised exception becomes
  `{"error": "..."}` in the observation, so the state machine stays in control.
- **Sextant** (`sextant.py`) — verification, in a fixed order: (1) `observation.error` ⇒ fail;
  (2) the action supplied its own `self_check` verdict ⇒ trust it; (3) the step declares structured
  `checks` ⇒ run the check registry, deterministic checks first, `llm_rubric` last; (4) legacy path
  for steps with no `checks` — deterministic action allowlist, then skill output contract, then the
  free-text acceptance criterion judged by an LLM, and finally "there was output and no criterion,
  so pass". LLM judging retries up to 3 times on unparseable JSON, then fails with a clear reason.

The engine is a plain async loop inside one ARQ job. A `requires_gate` step ends the job; approval
enqueues a new one. Because everything the loop needs is in the database, the job can also just die
and be picked up later ([§4.8](#48-scheduled-triggers-and-recovery)).

### 4.2 The action registry

Actions live in `app/agents/voyage/actions*.py` and register themselves with a decorator:

```python
@register("daily.fetch")
async def fetch(ctx: ActionContext, params: dict[str, Any]) -> dict[str, Any]:
    ...
```

The registry is a module-level dict. `get_action(name)` is what Helm calls; `known_actions()` is what
Navigator uses to constrain the LLM's action vocabulary and what `validate_steps()` checks a plan
against.

An action gets exactly one argument besides its params: `ActionContext`.

| On `ctx` | What it gives you |
| --- | --- |
| `ctx.run` | The `VoyageRun` row — `goal`, `kind`, `created_by`, `project_id`, `library_id`, `id`. |
| `ctx.llm` | The LLM router. Pass `voyage_id=ctx.run.id` (plus user/project/library) so the call is billed and attributed correctly. |
| `ctx.checkpoint` | A mutable dict. Whatever you leave here is persisted after the step and handed back on the next one. |
| `ctx.bus` | Event bus, may be `None`. |
| `ctx.step_id` | The current step's id, injected by the engine (used to attach logs to a step, and by actions that look up their own gate). |
| `ctx.notify(msg)` | Publish to the topic's notification channel. No-op when there is no bus or no `project_id`. |
| `ctx.log(text, level=…)` | Publish one progress line to the task terminal (and persist it). Use it for per-item progress in batch steps. |
| `ctx.skill_guidance(*targets)` / `ctx.skill_personas(target)` | Extra instructions and personas from the skill snapshot taken at run start. |

Actions do **not** get a database session. Each one opens its own via `get_sessionmaker()` and
commits itself — steps can run for a long time, so holding the engine's session would be wrong.

Return value convention:

- Anything you return becomes `step.observation`.
- A key named `error` means the step crashed; the engine may retry it in place.
- `usage` is added to the run's token totals.
- `self_check: {passed, reason}` bypasses Sextant.
- `plan_signal: {...}` is fed to the kind's branch table after the step passes, which can append or
  obsolete steps.

**To add an action, touch these files:**

1. Write the function in an `actions_*.py` module (new or existing) with `@register("your.action")`.
2. **Import that module in `app/agents/voyage/__init__.py`.** The registry is populated by import
   side-effect. A module that nobody imports registers nothing, and every step using the action fails
   with `unknown action: your.action`. That file is nothing but a list of `noqa: F401` imports for
   exactly this reason.
3. If the action belongs to a fixed plan, add the step to the relevant `*_plan()` function in
   `navigator.py`, with its `checks`.
4. If it should be verifiable without an LLM, either declare `checks` on the step or return
   `self_check` from the action.

`validate_steps()` supplies a default: a step with no declared `checks` whose action is not one of
`llm.complete` / `sleep` / `artifact.write` gets `[{"kind": "no_error"}]` automatically. Platform
batch actions therefore pass as long as they do not report an error.

### 4.3 Acceptance checks

`checks.py` is a small registry of verdict functions. A step's `acceptance.checks` is a list of
objects with a `kind`:

| Check | Passes when |
| --- | --- |
| `no_error` | `observation` has no `error` key. |
| `exit_code` | `observation.exit_code` equals `value` (default 0). |
| `artifact_exists` | The dotted path `key` resolves to something non-empty in the run's `checkpoint`. |
| `schema_valid` | `observation[field]` is an object containing every name in `required_keys`. |
| `metric` | `observation.metrics[name]` satisfies `op value` (`>=`, `<=`, `>`, `<`, `==`). |
| `min_count` | `observation[field]` counts (length, or the number itself) at least `value`. |
| `llm_rubric` | An LLM, given the step output and the `rubric` text, answers yes. |

Everything except `llm_rubric` is deterministic. `run_deterministic_checks()` runs those first and
short-circuits: the first failure returns a verdict immediately and **no LLM call is made**. Only if
all deterministic checks pass and at least one `llm_rubric` remains does Sextant call the model.

Failure reasons are deliberately specific (`[metric] 指标 accuracy = 0.62，不满足 >= 0.8`) because
that string is the diagnosis fed back into the retry params and into the replanning prompt. A vague
reason makes the retry useless.

`no_error` is the workhorse: nearly every step in the fixed pipelines declares only
`[{"kind": "no_error"}]`, which means "the action decides whether it worked; if it says it failed,
the step failed". That is why actions like `daily.fetch` bother to set `result["error"]` when all
subscribed categories came back empty — it converts a silent no-op into a visible, retryable failure.
The same rule read the other way is why `daily.embed` puts its failure in `embed_error` instead:
embedding is an optional extra and should not sink the whole run.

`done_criteria` on the run uses the same check format, evaluated once in `_finalize()` against the
checkpoint (there is no observation at that point). If it fails, the run goes to `paused_error`
rather than `done` — better to stop and ask a human than to declare success early.

### 4.4 Checkpoints

`run.checkpoint` is one JSON column that serves as the run's whole working memory. The engine copies
it into `ActionContext` before a step and copies it back after, then commits — so a value written by
step 2 is readable by step 5, and after a crash the next attempt sees exactly what the last commit
saw.

Known keys:

| Key | Written by | Purpose |
| --- | --- | --- |
| `params` | the creator of the run | Launch arguments (ingest knobs, manuscript id, section list, experiment id, …). Also passed to Navigator as planning context. |
| `skills` | engine, once per run | Snapshot of the topic's enabled skills. The run reads only this snapshot afterwards, so editing a skill mid-run changes nothing, and you can replay which skill version was used. Empty for runs with no topic. |
| `artifacts` | `artifact.write` and friends | Named text products, addressable by `artifact_exists` checks. |
| `gates` | engine | `{step_id: {gate_id}}`, so a resumed run knows which approval belongs to which step. Legacy rows keyed by cursor are still read as a fallback. |
| `gate_payload` | actions | Business context merged into the next gate that gets created (e.g. experiment id and budget summary). |
| `replans` | engine | Replan / plan-edit counter, capped at `MAX_REPLANS = 2`. |
| `replaced_steps` | engine | Serialised copies of steps that were obsoleted by a replan. |
| `plan_history` | engine | Append-only log of plan changes: `{iteration, source, reason, added, obsoleted, trigger_step, at}` where `source` is `signal` \| `navigator` \| `template` \| `budget`. Surfaced in the API as `plan_history`. |
| kind-specific keys | actions | e.g. `daily_entries`, `daily_touched_papers`, `watermark_candidate`, `compiled_count`, `iterate.stopped_reason`, `report_done`. |

Actions are expected to clean up after themselves — `daily.upsert` pops `daily_entries` once the
papers are in the pool, so the column does not carry a payload nobody needs any more.

Resume rebuilds nothing else. Because steps carry their own status and the checkpoint carries the
data, `resume()` is just "reset the broken steps, drive again". Long-running external work is
expected to re-attach rather than restart: the experiment actions, for example, reconnect to a remote
process that is still running instead of launching a second one.

### 4.5 Budgets

`run.budget.max_tokens` is the only limit the engine enforces. `run.usage` is updated after each step
from two sources — the tokens reported by the action and the verifier, and a `SUM` over the
`llm_usage` ledger rows tagged with this `voyage_id` — and the larger of the two is kept, because
batch actions do not always report usage while the ledger does not always capture estimated calls.

A falsy `max_tokens` (missing or `None`) disables the check entirely. That is intentional: ingest's
"unlimited" knob sets `{"max_tokens": None}`, and so does `daily_feed_sync`, whose only paid step is
optional embedding.

**When the budget runs out**, the engine does not simply stop (`_budget_finishing_steps`):

1. If any pending step is flagged `wrapup: true` (final compile, tournament summary, report), those
   steps are allowed to run and everything else pending is marked `obsolete`, with a `plan_history`
   entry of source `budget`. The point is to turn expensive completed work into a deliverable rather
   than lose it on the last cheap step.
2. If nothing is flagged but something has already passed, the last pending step is treated as an
   implicit wrap-up (this keeps runs created before the `wrapup` flag existed working).
3. If nothing has been completed at all, there is nothing honest to wrap up: the run goes to
   `paused_error`.

**Library monthly budgets** fold into the same mechanism instead of adding a second one
(`apply_library_budget` in `app/services/ingest.py`). When an ingest run is created:

- `derive_budget(knobs)` gives a starting figure — `max_papers × 20 000` tokens, or `None` for
  unlimited;
- `monthly_library_usage()` sums this calendar month's (UTC) `llm_usage` rows for this `library_id`;
- if `monthly_budget - used <= 0`, creation is refused with `LibraryBudgetExhaustedError`, which the
  API returns as `409 LIBRARY_BUDGET_EXHAUSTED` — the run is never created;
- otherwise `max_tokens` is tightened to `min(derived, remaining)`.

So a library budget behaves as: refuse to start when it is already spent, and otherwise cap this run
so it cannot overshoot the month. Once running, it is just an ordinary token budget and hits the
wrap-up path above. The cap resets on the first of the month.

### 4.6 Progress, events and logs

Everything the engine publishes goes to the Redis channel `voyage:{run_id}:events` and is forwarded
verbatim by `GET /voyages/{id}/events` as SSE. The stream replays the current status first, sends a
`: ping` comment every 15 s, and closes as soon as a terminal status goes by. A run that is already
terminal when you connect gets one status frame and then the stream ends.

| Event | Payload | Persisted? |
| --- | --- | --- |
| `status` | `{status, cursor}` | no |
| `step` | the full serialised step | no (but the step row itself is in the DB) |
| `log` | `{message, level, at, step_id?}` | **yes**, as a `log` row |
| `llm_start` / `llm_end` | `{stage}` | no |
| `llm_delta` | `{stage, delta, seq}` | **no** — throttled token increments, live only |
| the completed model output | — | **yes**, as an `llm` row, written when the stream ends |

So the rule is: the terminal you see live is SSE; the terminal you see after a refresh is
`GET /voyages/{id}/logs`, reconstructed from the `log` and `llm` rows in id order. High-frequency
increments are deliberately not persisted — they would be the bulk of the volume and add nothing once
the full output is stored.

Run status changes are additionally published to the topic notification channel
(`notify:project:{project_id}`) as `voyage.status`, and gate creation as `gate.created`. Runs with no
topic skip this silently — there is no channel to publish to.

The task detail endpoint also exposes two derived views built from the checkpoint: `skills` (which
skill versions this run snapshotted) and `plan_history` (every plan change, in plain language). By
default the detail response hides `obsolete` steps; pass `include_obsolete=true` to get them.

### 4.7 Failure handling, end to end

`_handle_failure()` in order:

1. **Crash within the attempt budget** — `observation.error` is set and `attempt < max_attempts`.
   The reason is written into `params["diagnosis"]` (truncated to 2 000 chars), the step is reset to
   `pending`, and the loop retries it. `max_attempts` defaults to 1 for pipeline and 2 for
   template/loop, overridable per step.
2. **`on_failure: "fail"`** — the step declared that failing is fatal. The run goes straight to
   `failed`, no replanning. Every step in `paper_writing`, `paper_review` and `presentation` sets
   this, as does `experiment.smoke` (which already runs its own internal LLM repair loop, so a
   failure there means the generated code is fundamentally broken and pretending otherwise would
   waste GPU time).
3. **pipeline** — no LLM replanning, by design. The run goes to `paused_error` with the diagnosis in
   the log. This is the "a human looks at it, fixes the cause, and hits retry" state: earlier steps
   keep their results, and `POST /voyages/{id}/resume` picks up from the broken step.
4. **template** — `Navigator.replan()`. For `idea_proposal` this is a hard-coded branch table
   (`proposal_replan`): a `DUPLICATE` novelty verdict inserts a direction-change approval and reruns
   from design; `NEEDS_DIFFERENTIATION` reruns from design with the diagnosis attached; anything else
   reruns from the failed step. Only kinds with no branch table fall through to the LLM. The tail
   from the failed step is marked `obsolete` (never deleted) and new steps are appended with fresh
   `seq` values.
5. **loop** — `Navigator.on_result()` returns an incremental *plan edit* (`add_nodes` /
   `update_node` / `obsolete_nodes`), validated against the action registry, a max of 8 new steps,
   and the rule that only unfinished steps may be referenced. Applying it obsoletes the failed step
   automatically. If the model says `finish`, returns nothing usable, or the edit is rejected, the
   run goes to `paused_error` — "no progress" is never allowed to look like success.

Both 4 and 5 stop after `MAX_REPLANS = 2` and land in `paused_error`.

Invariants held while editing a plan: `seq` only ever increases and is never rewritten; `rank` takes
a value in the gap between neighbours; nothing may be inserted before the current execution point;
passed steps can be neither edited nor obsoleted.

### 4.8 Scheduled triggers and recovery

`src/backend/worker/settings.py` registers three cron jobs. All times are **UTC**.

| Time (UTC) | Job | What it does |
| --- | --- | --- |
| 01:30 | `daily_feed_sync` | Creates one `daily_feed_sync` task and enqueues it. arXiv publishes new announcements around 00:00 UTC; 01:30 leaves it time to settle and stays clear of the 03:00 job. Globally single-flight — if a fetch is already running it returns `None` and skips. |
| 03:00 | `daily_wiki_ingest` | For every active topic whose library has `cadence=daily` and has already been built (has a sync marker), creates a `wiki_ingest` task and enqueues it. Time comes from `DAILY_SYNC_UTC_HOUR/MINUTE` in `app/services/ingest.py`, so the cron and the "next sync at" shown in the UI cannot drift apart. |
| 04:00 | `daily_publication_match` | Not a task-system job — scans the library for publication matches for users who opted in. Scheduled at `DAILY_SYNC_UTC_HOUR + 1` so it runs after the incremental ingest. |

Two more operational details:

- **Job timeout.** `run_voyage` and `resume_voyage` get a 12-hour timeout; everything else keeps
  ARQ's 1-hour default. The default was actively harmful: a GPU training round legitimately runs for
  hours, ARQ would kill the polling job, then retry it with an exponential backoff keyed on job age,
  and the run would sit idle for hours. The in-run budget is the real guard, not the job timeout.
- **Startup reconcile.** `reconcile_stuck_voyages` runs `on_startup`: every run still marked
  `executing` is re-enqueued as `resume_voyage`, deduplicated by a fixed `_job_id`. This is the net
  under a worker that was killed mid-run. It is safe because the engine is idempotent — steps
  re-attach to work that is still running, and the checkpoint restores the rest.

---

## 5. Two worked examples

### 5.1 Building a library — `wiki_ingest`

**Trigger.** Someone hits `POST /libraries/{id}/ingest/run` (or `POST /projects/{id}/ingest`), or the
03:00 cron picks the library up. `create_ingest_voyage()`:

1. refuses with `IngestConflictError` → `409 INGEST_ALREADY_RUNNING` if a non-terminal
   `wiki_bootstrap`/`wiki_ingest` run already exists **for this library** (mutual exclusion is keyed
   on the library, not the topic, precisely because library runs do not carry a `project_id`);
2. derives the token budget from the knobs and narrows it by the library's remaining monthly budget,
   refusing outright if the month is spent;
3. writes a `VoyageRun` with `kind="wiki_ingest"`, `library_id` set, **`project_id` left null**, and
   `checkpoint["params"] = {mode, knobs}`;
4. writes an `ingest.started` activity row against the library;
5. the caller enqueues `run_voyage`.

**Planning.** Mode is `pipeline`. `Navigator.plan()` returns `wiki_plan()` — seven fixed steps, each
with `checks: [{"kind": "no_error"}]`, no approvals:

| # | Action | What it does |
| --- | --- | --- |
| 1 | `wiki.search_candidates` | Reads the library's own definition (arXiv categories, include keywords) and its sync marker; incremental mode searches from `watermark - lookback`, bootstrap mode from `months_back` ago. Deduplicates against three keys (arXiv id / DOI / normalised title) and against the global content pool, then creates `candidate` membership rows. |
| 2 | `wiki.snowball` | Expands through references (Semantic Scholar). |
| 3 | `wiki.score_relevance` | Scores each candidate against the library definition with an LLM, one paper per session, and moves it to `scored` or `excluded`. |
| 4 | `wiki.fetch_extract` | Downloads PDFs and extracts full text for the top N, plus figures; degrades to abstract-only on failure. |
| 5 | `wiki.compile` | Writes the illustrated per-paper intro into the membership row's `wiki_content`. |
| 6 | `wiki.link_concepts` | Extracts and links canonical concepts, and fills in any missing paper-level and chunk vectors. |
| 7 | `wiki.update_watermark` | Writes `library.ingest_state = {watermark, last_run: {voyage_id, finished_at}}` and an `ingest.completed` activity row. |

Steps 1–6 hand values forward through `ctx.checkpoint` (`watermark_candidate`, `compiled_count`, …)
and call `ctx.log()` per paper, so the terminal shows "scoring 12/50: <title>" rather than sitting
mute for twenty minutes. Billing follows library ownership: a public library uses the system key, a
personal one uses the creator's, and either way tokens are recorded against `library_id`.

**If it breaks.** Say arXiv is unreachable and step 1 returns an `error`. Pipeline mode allows 1
attempt, so there is no in-place retry; there is no `on_failure: "fail"`, so the run goes to
`paused_error`. It is visible in the lab workspace under the library's group with the reason in the
log. Once the network is back, `POST /voyages/{id}/resume` resets that step to `pending` and drives
on. If it dies at step 5 instead, steps 1–4 are `passed` and are not redone — the crawl and the
scoring are not repeated.

**Visibility.** `project_id` is null, so this run never appears in a topic's task list. It is visible
in the list to anyone in a topic that links the library, to its curators, and to admins; the detail
page additionally requires manage rights on the library (creator / curator / admin).

### 5.2 Daily new papers — `daily_feed_sync`

**Trigger.** The 01:30 UTC cron, or an admin hitting `POST /daily/refresh`.
`create_daily_feed_voyage()` is single-flight globally (`409 DAILY_FEED_RUNNING` if one is already
running — deliberately stricter than a per-day job id, so a failed run can be retried immediately
while a running one can never be double-triggered). The row it writes has **no `project_id` and no
`library_id`**, and `budget = {"max_tokens": None}` because only the last step spends anything.

**Planning.** Mode is `pipeline`; `daily_feed_plan()` is four fixed steps, each with `no_error`:

| # | Action | What it does |
| --- | --- | --- |
| 1 | `daily.fetch` | Pulls each subscribed category's new announcements, logs a per-category count, and leaves the entries in `checkpoint["daily_entries"]`. Sets `error` if there are subscribed categories but **every** one came back empty — one empty category is normal on a weekend, all of them empty means arXiv is down or the config is wrong. |
| 2 | `daily.upsert` | Deduplicates into the content pool and creates/merges feed entries; records the touched paper ids in `checkpoint["daily_touched_papers"]` and drops `daily_entries`. |
| 3 | `daily.cleanup` | Expires entries outside the rolling 7-day window and reclaims papers nobody collected. |
| 4 | `daily.embed` | Only if the admin setting `daily_feed_embed_enabled` is on: builds paper-level vectors for the papers touched in step 2. Deliberately best-effort — a failure is reported as `embed_error`, *not* `error`, so it cannot fail the run. |

Handing the fetched entries forward through the checkpoint rather than re-fetching in step 2 is not
just an optimisation: re-querying arXiv could return a different result set, and step 2's dedup keys
would no longer correspond to what step 1 reported.

**Why it went into the task system at all.** These four things used to run as a bare worker function
where failures were swallowed. As a task they get a plan, per-step status, a terminal, an entry in
the list, and a retry button. The direct function `sync_daily_feed()` still exists in
`app/services/daily_feed.py` for scripts and tests, and shares the same step functions, but nothing
in production calls it.

**Visibility.** Both scope ids are null, so only platform admins can see it — in the lab workspace's
"other tasks" group. That matches the rest of the daily feed's admin surface (category subscriptions,
manual refresh, the embedding toggle). A non-admin sees the papers, not the machinery.

---

## 6. Sharp edges

- **A new action that nobody imports does not exist.** If `app/agents/voyage/__init__.py` does not
  import the module, `@register` never runs, and every step using the action fails with
  `unknown action: …`.
- **`gates.project_id` is `NOT NULL`.** A step with `requires_gate` inside a run that has no topic
  would fail on insert. No library-level or platform-level kind declares a gate today, so this is
  latent rather than broken — but it is a constraint to respect when adding gated steps to library
  tasks.
- **`run.plan` is a snapshot, not the source of truth.** Write plan changes as step rows and call
  `_regen_plan_snapshot()`; editing `run.plan` directly will be silently overwritten.
- **Streamed events are not history.** Anything that has to survive a refresh must land in the
  database: log rows, `step.attempts`, `checkpoint["plan_history"]`.
- **Many `docs/…` paths in the source comments do not exist.** The voyage modules reference
  `docs/voyage-loop.md`, `docs/api-m1.md`, `docs/api-m2.md`, `docs/api-m3.md`, `docs/api-idea2.md`,
  `docs/api-m5-b.md`, `docs/api-m5-c.md`, `docs/skill-system.md` and others. Those are internal
  design notes that were never published to `docs/`. Treat them as historical labels for a design
  decision, not as links.
