# PolarisBuddy

PolarisBuddy is the global assistant that rides along on every page of Polaris. Press **⌘J**
(**Ctrl+J** on Windows/Linux) anywhere — or click the floating bubble in the corner — and a chat
panel opens beside whatever you were doing. Unlike the six purpose-built chat surfaces (paper chat,
library chat, and so on), Buddy is not tied to one page: it can call the platform's whole read-only
tool registry, so it answers questions that cut across papers, libraries, ideas, experiments, and
manuscripts.

Buddy runs the same Navigator / Helm / Sextant split as a [Voyage](concepts.md), scaled down to a
conversation: a Navigator decides how many tool rounds are left, a Helm executes tool calls in
parallel, and a Sextant sanity-checks the finished answer. Every step of that loop is visible in the
panel as it happens.

<!-- screenshot: the Buddy panel docked on the right of a paper reading page, showing a streamed answer with tool cards and a "Papers looked at" list -->

## Opening it

- **⌘J / Ctrl+J** toggles the panel from any page.
- The **floating bubble** does the same on click. It is draggable, remembers its position, and is
  also a drop target (see below). Its tooltip reads "PolarisBuddy (⌘J) · drag me · drop a paper or
  selected text on me".
- On wide screens the panel docks as a column next to your work; on narrow screens it becomes an
  overlay drawer. Same content either way.

When the panel opens on an empty conversation, Buddy shows a time-of-day greeting, an **opening
question**, and four suggestion cards. The question and cards are chosen from what is true right
now — first from the page you are on, otherwise from your own recent activity (running experiments,
this week's ideas and saved papers, papers you left half-read, today's feed). None of this goes
through a model: the signals come straight from SQL and the sentences are picked, not generated, so
the opening is instant, free, and never wrong. When there is genuinely something new — an experiment
running, fresh papers in today's feed — the closed bubble can also show a one-line nudge; no news, no
nudge.

## Page awareness

Buddy knows what you are looking at. The frontend derives a page context from the current route —
reading a paper, viewing an idea, watching an experiment, browsing a library, writing a manuscript,
sitting in a topic workspace, or scanning the daily feed — and sends it along with each question. Two
things follow:

- The **opening cards change with the page**. On a paper you get "Explain this paper", "Compare with
  prior work", "Is the evidence strong enough?"; on an experiment you get "Where is it up to?",
  "Can the results be trusted?"; and so on.
- When you say "this paper" or "this experiment" mid-conversation, Buddy resolves it to the thing on
  your screen.

::: tip Context is a hint, not a permission
The page context only tells the model what you are looking at. When Buddy actually reads content, it
goes through the same tools and the same permission checks as everything else — a forged id can only
make Buddy look up something you were already allowed to see.
:::

## Drag a paper in

Drag a paper card from anywhere in the platform onto the floating bubble. The bubble highlights, and
on drop Buddy immediately asks itself to walk you through that paper — the problem, how the method
works, how strong the evidence is — using the platform's stored full text and wiki notes.

You can also drag **selected text** onto the bubble: Buddy explains the passage, checking the
platform's corpus where needed.

## How a turn runs

Each question starts a tool loop of up to 8 model⇄tool rounds:

1. The model streams its thinking and answer text. A collapsed "Thinking" row shows the latest line
   while it reasons.
2. When it decides to call tools, the panel shows a **tool card per call** — name, a one-line
   summary of what came back, and duration; click to expand a result preview. Read-only calls run in
   parallel (up to 4 at a time, 60 s each).
3. Results are fed back and the loop continues until the model answers, the round limit is reached
   (the loop then forces a wrap-up with the material already gathered), or you press stop.

Alongside the answer you get:

- **Papers looked at** — every paper any tool returned this turn (up to 20), each linked to its
  reading page. This is deliberately *not* labelled "citations": the platform cannot verify which
  paper each `[n]` marker in the prose refers to, so it shows you what was actually retrieved and
  lets you judge.
- **Inline figures and citation chips** — the model can embed platform figures and numbered
  references directly in the answer; figures render inline, and `[n]` chips link to papers.
- **A verification note** when something does not line up — for example the answer cites `[5]` but
  only 3 papers were retrieved, or it claims "I found…" without a single successful search. These
  checks are deterministic and heuristic; they flag, they never rewrite or block.
- **Follow-up chips** derived from what actually happened this turn.

### Which tools it has

The platform keeps a single registry of 47 tools (the same registry the [MCP server](mcp.md)
exposes). Buddy gets **every read-only tool by default** — around 43 once a couple of exceptions are
removed — spanning literature search, full-text grep, wiki notes, figures, concepts, the knowledge
graph, ideas, experiments and their logs, tasks, gates, manuscripts, and external lookups (arXiv,
Semantic Scholar, OpenAlex). Enabling memory adds `remember`/`recall`; plan mode adds `submit_plan`.
Buddy cannot write to anything except its own opt-in memory.

### Scope

The bar above the input box shows what this question can reach. By default it follows the topic you
are working in; click it to pick another topic, or choose **All assets** to search everything you can
see. Lab-level assets (the daily feed, public libraries) are always included — they belong to no
topic. Once you pick a scope by hand, it stays put instead of following your navigation. Each
conversation remembers the scope it was asked in.

## Modes

Open the **+** menu next to the scope picker to choose one of three modes:

| Mode | UI label | Behavior |
| --- | --- | --- |
| Normal | "Normal · just answer" | The default. Ask, get an answer. |
| Plan | "Plan · propose first, act on approval" | This turn does research only — no acting. Buddy investigates with tools, then submits a step-by-step plan and stops. |
| Goal | "Goal mode · keep pursuing one goal" | Hand it an objective and it drives toward a result across turns. |

**Plan mode** ends its turn with a plan card titled "Shall I go ahead?" and two buttons: **Go
ahead** and **Revise it**. Approving is one click — it switches back to normal mode and tells the
model to execute the plan it just proposed, updating a live progress card (`n/m` steps, running step
highlighted) as it goes. "Revise it" hands the floor back to you to say what is wrong. The point of
the mode is to move the "I thought you wanted A" conversation to *before* any time or tokens are
spent.

**Goal mode** takes your **first message as the standing goal** — no separate field to fill in. The
goal is stored on the conversation and re-sent every round, so the model cannot forget it by turn
three. In this mode Buddy is instructed to report progress against the goal each round, keep moving
on its own (look things up rather than asking "which should I check first?"), stop only for genuine
decisions — direction, spend, anything that would change data — and to say plainly when the goal is
reached. Click the mode label to drop back to plain chat.

<!-- screenshot: plan mode showing a submitted plan card with the "Go ahead" and "Revise it" buttons -->

## Conversations

- Buddy keeps **named conversations**. A title is generated after the first turn; the conversation
  rail (dock layout) or the history popover (overlay layout) lists them, newest activity first.
- Conversations run **in parallel**: you can start a turn, switch to another conversation, and come
  back — a live dot marks every conversation that is still running, and stop only stops the one you
  press it in.
- Stopping or losing the connection mid-answer keeps everything generated so far, marked as
  interrupted.
- Replayed history is budgeted to roughly half the model's context window, and older tool results
  are compacted to one-line stubs after a couple of rounds — long conversations stay cheap, but very
  old retrieval detail is summarized away. Start a new conversation for a new subject.

## Memory

Buddy can keep long-term memory about you — research direction, preferences, standing agreements —
but it is **off by default**: an assistant that records things should be switched on by its user.
Enable it in **Settings → PolarisBuddy → Memory**. Once on:

- Buddy gets two extra tools, `remember` and `recall` — the only writing tools it ever has, and they
  touch nothing but its own notes about you.
- Stable **facts** are injected into every turn (capped at ~1 200 characters, newest first);
  timestamped **notes** surface only when recalled.
- Every memory is visible and deletable in the same settings card.

## Key settings

| Setting | Where | What it does |
| --- | --- | --- |
| Assistant enabled | admin settings | Deployment-wide switch. When off, the panel explains that an admin must enable it. |
| Memory | Settings → PolarisBuddy | Opt-in long-term memory; list and delete individual memories. |
| Skills | Settings → PolarisBuddy | The on-demand skill playbooks Buddy can load; built-ins plus your own. See [Skills](skills.md). |
| MCP | Settings → PolarisBuddy | The same tool registry exposed to external clients. See [MCP](mcp.md). |
| Scope / mode | in the panel | Per-conversation topic scope and chat/plan/goal mode. |

## Tips and limits

- **Buddy is read-only.** It can look at everything you can see, but it cannot edit papers, approve
  [gates](concepts.md#approval-gates), start experiments, or change manuscripts. Long, stateful work
  belongs to [tasks](task-system.md); Buddy can inspect their status and logs.
- **8 tool rounds per turn.** Sprawling questions ("survey this whole field") are better asked in
  goal mode, or phrased so Buddy can delegate to its research sub-agent, which keeps intermediate
  retrieval out of your conversation.
- **Tool results are truncated** (about 4 000 characters each fed back to the model), so "read me
  the entire paper" works better as targeted questions.
- **The "Papers looked at" list is retrieval, not citation.** If a claim matters, click through and
  check — the verification note will warn you about the most obvious mismatches, but it is a
  heuristic, not a guarantee.
- If your model provider does not support function calling, Buddy degrades to answering from
  context alone and says so, rather than failing the turn.
