# Skills

A skill is agent behavior packaged as **data, not code**. The judgemental instructions that would
otherwise be hard-coded into Polaris's agents — scoring rubrics, reviewer personas, writing
conventions, retrieval playbooks — live as versioned, shareable records that you can read, edit,
test, and swap without touching the platform. Skills change how the AI *judges*; they can never
bypass the guardrails written in code (experiment numbers must come from real runs, citations must
map to real papers, a skill can never grant a tool the session did not already have).

Polaris has two skill layers, for its two kinds of agents:

| Layer | Used by | Shape | Where you manage it |
| --- | --- | --- | --- |
| **Task skills** | [Voyage tasks](concepts.md) — the pipeline stages | Structured record with a kind (guidance / rubric / persona / workflow) and target stages | **/skills** (the skill library and market) |
| **Assistant skills** | [PolarisBuddy](buddy.md) | A `SKILL.md` file — description, body, optional attachments | Settings → PolarisBuddy → Skills |

## Task skills

### The four kinds

| Kind | UI label | Effect |
| --- | --- | --- |
| `guidance` | Guidance | Appends extra instructions to a stage's prompt — a writing convention, an experiment code checklist, a note-taking standard. |
| `rubric` | Rubric | Sharpens a stage's scoring criteria — literature relevance anchors, idea scoring, top-venue review standards. |
| `persona` | Reviewer persona | A pack of debater/reviewer personas consumed by the idea-debate and paper-review stages; enabling one replaces the built-in default personas. |
| `workflow` | Workflow template | A ready-made step plan (the same step schema [Navigator](concepts.md#navigator--planning) uses) that can be run directly as an AI task. |

### Where skills attach

Every skill declares one or more **target stages** — the named points in the pipeline where its
content is injected into the AI's instructions. The catalog today:

| Target | Stage |
| --- | --- |
| `wiki.score_relevance` | Paper relevance scoring |
| `wiki.compile` | Paper note compilation |
| `forge.gap_analysis` | Research gap analysis |
| `forge.generate` | Idea generation |
| `forge.score` | Idea scoring |
| `review.debate` | Idea debate |
| `review.referees` | Paper referees |
| `review.meta_review` | Review synthesis |
| `experiment.plan` / `experiment.setup` / `experiment.iterate` / `experiment.report` | Experiment planning, setup, iteration, report |
| `writing.section` | Paper section writing |
| `writing.related_work` | Related-work survey |
| `navigator.free_plan` | Free-form task planning (workflow skills) |

So a review standard attaches to `review.referees`, a debate persona pack to `review.debate`, and a
code convention to `experiment.setup` — each pipeline stage picks up exactly the skills aimed at it.
Several skills on the same target are injected in your chosen order.

### Enabling and reproducibility

Skills are enabled **globally per user** (they are not bound to one topic): pick a skill, pick a
target stage, optionally pin a specific version. From then on, every new task you start snapshots
your enabled skills into the run itself. Three consequences:

- Editing a skill mid-run changes nothing for runs already in flight.
- A task's detail page shows exactly which skill versions it used — replays are honest.
- Deleting a skill affects future runs only.

### Built-in skills

Polaris ships fifteen built-in task skills covering the whole pipeline, including: a literature
relevance rubric, a reading-note style guide, six gap-analysis lenses, a four-axis idea scoring
rubric with anchor definitions, an idea-generation quality bar, a top-venue review rubric, academic /
abstract / related-work writing guides, classic debate and strict referee persona packs, an
experiment design checklist, a reproducible-code convention, and two workflow templates (a
literature-review sketch and a rebuttal draft).

Built-ins are read-only. To adapt one, use **Copy to my skills** — the copy is yours to edit.

### Working with skills

On the **/skills** page (Library tab):

1. **Browse** by kind and scope (built-in / mine), or search.
2. **Test run** a guidance or rubric skill: the platform renders exactly what would be injected and
   makes one real model call against a sample goal so you can see the effect before enabling.
   Persona and workflow skills show a structure preview instead.
3. **Enable** it on a target stage. Each skill declares which stages it accepts.
4. **Edit (save as new version)** — skills are append-only versioned; enablements pinned to an older
   version are unaffected, unpinned ones pick up the latest.
5. **Run this workflow** (workflow skills only): enter a goal and it creates an AI task whose plan is
   the template's steps, with `{goal}` substituted into each step's prompt. The task runs in the
   normal [task system](task-system.md) with the full console, gates, and budget machinery.
6. **Export** a skill as a single-file JSON pack (`polaris-skill@1`) to share across deployments;
   import works the same way in reverse.

<!-- screenshot: the /skills library tab showing built-in skill cards with kind pills, and the detail dialog with Enable / Test run / Copy to my skills -->

### The skill market

The Market tab is deployment-internal sharing:

- **Publish to market** submits your skill's current version for review; an admin approves or
  rejects it.
- Approved listings can be **browsed, searched, and sorted** (newest or most-installed), **installed
  to my skills** (installing copies the published version as your own editable skill), and **rated**
  with a comment.
- A listing always points at the exact version that was published — later edits to your skill do not
  silently change what others install.

## Assistant skills (Buddy)

PolarisBuddy uses a second, lighter-weight skill shape — a `SKILL.md` file with YAML frontmatter —
built around **progressive disclosure**, because Buddy pays for its prompt on every round:

1. **Catalog line** (always present): each skill contributes one line — its name and *when to use
   it* — to Buddy's system prompt. The whole catalog is capped at 4 000 characters; the most-loaded
   skills survive truncation.
2. **Body on demand**: when the description matches the situation, Buddy calls the `skill_load` tool
   to pull in the full playbook (up to 64 KB of markdown).
3. **Attachments on demand**: a skill can carry files — templates to copy, reference tables — that
   Buddy reads individually with `skill_read_file`.

The frontmatter fields:

```markdown
---
name: literature-triage
description: >
  Use when the user asks to bulk-screen a batch of papers against a direction:
  "filter these for me", "which of today's feed is worth reading".
allowed-tools: [scan_papers, search_papers, get_paper, read_wiki]
invocation: auto
---

# Triage procedure
…the actual playbook…
```

- `name` is a lowercase-hyphen slug; `description` is the **trigger condition**, not a feature
  blurb — it is the only thing the model sees before deciding to load, so write "use when…".
- `allowed-tools` narrows Buddy's tool set for the rest of the turn after the skill loads. This is
  intersection-only: **a skill can restrict tools, never add them** — naming a tool the session does
  not have simply leaves it unavailable.

Five assistant skills ship built in: a search-tool selection playbook, a literature triage procedure,
a deep-read template, a citation-hygiene discipline, and a research-landscape procedure that
delegates broad surveys to a sub-agent. Your own skills are created by POSTing a `SKILL.md` to
`POST /api/chat/skills` (same slug overwrites your copy; built-ins are untouchable), and everything
visible to you is listed — and yours deletable — under **Settings → PolarisBuddy → Skills**.

## Tips and limits

- **Skills are declarative.** No skill layer executes code. Task skills carry markdown, rubrics,
  personas, or whitelisted workflow steps; assistant skills carry markdown and static attachments.
- **Descriptions decide everything** for assistant skills: a skill whose description says what it
  *is* instead of *when to use it* will never be loaded.
- **Workflow steps are validated on save** against the task system's action registry, so a template
  cannot reference actions that do not exist.
- Editing never rewrites history: task skills version forward, tasks snapshot what they ran with,
  and market listings freeze the published version.
- Skill guidance is additive judgement — hard rules (gated writes, citation verification, budget
  caps) live in code and win regardless of what a skill says.
