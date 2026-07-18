# Contributing to Polaris

This is the short version of how we work. The full, authoritative guide (with
copy-paste commands) is [`docs/git-workflow.md`](docs/git-workflow.md).

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
- Run the roundtrip test before merging: `backend/tests/test_migrations.py`
  (`alembic upgrade head` + downgrade).

## Local preview without touching `main`

The docker dev stack mounts source via `DEV_SRC` (defaults to the main
checkout). To preview a branch, point `DEV_SRC` at a worktree:

```bash
DEV_SRC=../wt/dev docker compose \
  -f deploy/docker-compose.yml -f deploy/docker-compose.dev.yml up -d
```

Container `node_modules` is an anonymous volume, so preview worktrees don't need
a local `npm install`. See `docs/git-workflow.md` for the details.

## Deployment

Production (zju-54) deploys **only from `origin/main`** — never from a local
branch.
