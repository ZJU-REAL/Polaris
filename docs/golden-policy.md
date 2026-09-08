# Golden transcript policy

Polaris keeps two **golden transcripts** under `src/backend/tests/golden/data/`:

- `import_wiki.json` — register → create library → create project → bibtex import →
  wiki compile → index status (`tests/golden/test_golden_import_wiki.py`)
- `discovery_run.json` — the full hypothesis-discovery chain: papers → library index →
  discovery voyage → tree + disclosure artifacts (`tests/golden/test_golden_discovery.py`)

Each test replays its chain deterministically (fake LLM provider + SQLite, zero network)
and compares the normalized API responses **byte for byte** against the stored file.

## Why so strict

The goldens are a *behavior* gate, not a coverage gate. They pin the externally
observable wire shape of two end-to-end chains, so that refactors, removals and
"pure cleanup" PRs cannot silently change what the API says. A failing golden always
means one of two things:

1. **An accident** — you changed behavior you did not mean to change. Fix the code,
   not the golden.
2. **An intentional wire change** — then the golden must be re-recorded *in the same PR*,
   with the diff reviewed by a human and explained in the PR description.

The one thing that is never acceptable is re-recording to "make CI green" without
reading the diff. A golden diff you cannot explain line by line is an accident by
definition.

## When a re-record is legitimate

- The PR intentionally changes a response shape (adds/removes/renames a field on a
  route the chains touch), and the golden diff contains **exactly** those keys and
  nothing else.
- The PR intentionally changes deterministic behavior the chains exercise (e.g. a
  different plan template, a new pipeline step) and the diff matches the described
  change.

Not legitimate:

- Re-recording inside a PR that claims to be a pure removal/refactor ("no behavior
  change"). If the golden moved, the claim is false — investigate first.
- Diffs containing timestamps, ids or ordering churn: that is a normalization bug in
  `tests/golden/normalize.py`, not a behavior change. Fix the normalizer.

## How to re-record

From the repo root (runs in a one-shot container, same image as `polaris-api`):

```sh
make golden-record
```

This runs the golden tests with `POLARIS_GOLDEN=record`, which rewrites the files in
`src/backend/tests/golden/data/` in place. Then:

1. `git diff src/backend/tests/golden/data/` — read every hunk. Every changed key must
   map to a change you intended and can name.
2. Run the golden tests again *without* the flag (`pytest tests/golden -q`) — they must
   pass twice in a row, proving the recording is stable, not flaky.
3. Commit the golden files **together with** the code change, and paste a short diff
   summary (which keys appeared/disappeared and why) into the PR description.

CI only ever compares; it never records. The discipline is the reviewed diff.

## Retiring compatibility-only fields

Historically, dead fields were kept at constant values ("wire-shape compatibility")
precisely because there was no documented re-record procedure — the goldens
structurally kept ghost code alive (#715 item 14). That reason is gone: when a field
is dead, remove it and re-record under the rules above (#734 did this for
`UserRead.llm_self_managed` and `DirectionLibrarySummary.status/review_note`).
A golden must pin behavior, not embalm it.
