# Contributing to Polaris

This is the short version of how we work. See [`docs/development.md`](docs/development.md)
for the full local-development and workflow guide.

Issues and pull requests are written in **English**.

## Golden rules

1. **Never merge feature branches into `main`.** `main` is a read-only mirror of
   `origin/main` and only ever fast-forwards.
2. **Never `git merge main` into a feature branch** — rebase onto it instead, so
   unrelated work doesn't leak into your PR.
3. **Migrations use random revision ids** (`alembic revision -m "..."`), never
   hand-rolled rolling ids — parallel branches otherwise collide.

## Branch & PR flow

One feature = one branch = one worktree = one PR.

1. **Open an issue first** (bug / feature / task template).
2. Branch from the latest `origin/main`, named `feat/…`, `fix/…`, `chore/…`, or
   `docs/…`:
   ```bash
   git fetch origin
   git worktree add ../wt/feat-x -b feat/x origin/main
   ```
3. Commit with **conventional-commit** messages (`feat(scope): …`,
   `fix(scope): …`). No AI-attribution / `Co-Authored-By` trailers.
4. Open a **draft PR** whose description says `Closes #<issue>`:
   ```bash
   gh pr create --draft
   ```
5. Keep up with `main` by **rebasing**, not merging:
   ```bash
   git fetch origin && git rebase origin/main
   git push --force-with-lease
   ```
6. After merge, clean up: `git worktree remove ../wt/feat-x && git branch -d feat/x`.

## Migrations

- Generate with a random id (`alembic revision -m "..."`).
- Make sure `down_revision` chains onto the current `origin/main` head.
- Run the roundtrip test before merging: `src/backend/tests/test_migrations.py`
  (`alembic upgrade head` + downgrade).

## Local preview without touching `main`

Everything runs from source with no Docker: to preview a branch, run it from its
own worktree.

```bash
make venv && make backend-dev      # the engine on :8000, from this worktree
make frontend-dev                  # the frontend on :5173
```

See [`docs/development.md`](docs/development.md) for the details.

## Releases

Polaris ships as the desktop app only. Releases are cut **only from
`origin/main`** (a `v*` tag builds the installers) — never from a local branch.
