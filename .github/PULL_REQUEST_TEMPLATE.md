<!-- PR title: conventional-commit style, e.g. "feat(reading): zoom controls" -->

## Summary

<!-- One or two sentences: what does this PR do, and why? -->

Closes #

## Area

<!-- Tick what this touches. Keep it to what actually changed. -->

- [ ] frontend
- [ ] desktop (Electron client)
- [ ] backend-api
- [ ] voyage (task loop)
- [ ] literature / wiki
- [ ] daily papers
- [ ] idea forge
- [ ] experiment
- [ ] writer
- [ ] review
- [ ] skills
- [ ] llm routing / usage
- [ ] auth / users
- [ ] infra / deploy
- [ ] docs

## What changed

-

## Testing

<!-- Check what you actually ran/verified; delete rows that don't apply. -->

- [ ] `tsc --noEmit` / frontend build passes (frontend touched)
- [ ] Backend tests pass (backend touched)
- [ ] Alembic `upgrade head` + downgrade roundtrip passes (migration added)
- [ ] `alembic heads` shows a single head (migration added)
- [ ] Manually verified in local dev

## Checklist

- [ ] Branch is rebased on the latest `origin/main` (not merged from main)
- [ ] Conventional-commit PR title (`feat|fix|chore|docs(scope): …`)
- [ ] New migration (if any) uses a random revision id and chains onto the current head
- [ ] No AI attribution / `Co-Authored-By` lines in commits
- [ ] Screenshots attached for UI changes
