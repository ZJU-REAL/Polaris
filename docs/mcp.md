# MCP: driving Polaris from Claude Code, Codex, or Cursor

Polaris exposes its retrieval and workspace surface as an [MCP](https://modelcontextprotocol.io)
server, so an external coding agent can read your research workspace directly: search the papers
you have collected, read their wiki pages and full text, look at concepts and the knowledge graph,
see which AI tasks are running, what is waiting on a human decision, and how far a manuscript has
got.

**Everything exposed today is read-only.** No tool creates, edits, or deletes anything, and no tool
runs anything on a remote machine. An agent connected to this server can inform itself about your
work; it cannot change it. Writes (starting an ingest, forging ideas, launching an experiment,
drafting a paper) remain web-only for now.

---

## 1. Connect

Two transports, same tools, same permissions.

### Streamable HTTP (recommended)

One endpoint, `POST /mcp`, authenticated with your normal platform token — the same bearer token the
web app uses. Get yours from **Settings → MCP**, which shows the endpoint URL, your token, and a
ready-made config block to copy.

**Claude Code** — add it to `.mcp.json` in your project (or run `claude mcp add`):

```json
{
  "mcpServers": {
    "polaris": {
      "type": "http",
      "url": "https://polaris.example.edu/mcp",
      "headers": { "Authorization": "Bearer <YOUR_TOKEN>" }
    }
  }
}
```

**Cursor** and other HTTP-capable clients take the same shape.

**Codex** reads `~/.codex/config.toml`. If your version supports HTTP MCP servers:

```toml
[mcp_servers.polaris]
url = "https://polaris.example.edu/mcp"
http_headers = { Authorization = "Bearer <YOUR_TOKEN>" }
```

If it does not, use the stdio transport below.

### stdio

`python -m app.mcp` speaks JSON-RPC over stdin/stdout. It talks to the database directly, so it has
to run where the backend's environment is available — in practice inside the API container or a
checkout with the same `.env`. The user is taken from an environment variable rather than a token,
because a local process is treated as trusted:

```toml
[mcp_servers.polaris]
command = "docker"
args = ["exec", "-i", "-w", "/srv/backend", "polaris-api-1", "python", "-m", "app.mcp"]
env = { POLARIS_MCP_USER_EMAIL = "you@example.edu" }
```

`POLARIS_MCP_USER_EMAIL` must be a registered user; every call runs as that user.

### Scoping and permissions

Every tool call carries a `project_id` — the research topic you want the agent to look at. The
server verifies you are a member of that topic on every single call, and tools only ever see data
inside it: a task, manuscript, or library belonging to another topic is reported as not found even
when you personally have access to it elsewhere. Library visibility follows the platform's own rule
— your own personal libraries plus every shared library, nothing more.

Find your topic id in the web app's URL: `/t/<topic-id>`.

---

## 2. Tools

45 tools in nine groups. Names are stable; treat them as API.

### Papers and reading

| Tool | What it gives you |
| --- | --- |
| `search_papers` | Search papers in the topic's corpus. `mode=semantic` (default) falls back to keyword when embeddings are unavailable. |
| `scan_papers` | A cheap wide scan: titles and years only, up to 50 at a time. Use it to map the corpus before deep-reading a few papers. |
| `search_chunks` | Passage-level search — lands on the paragraph rather than the paper. |
| `grep_fulltext` | Literal string match across the full texts, with a small context window per hit. Better than semantic search for exact terms, model names, dataset names, or formula symbols. |
| `get_paper` | Metadata, authors, status, abstract, concept tags. |
| `read_wiki` | The platform's compiled reading note for a paper (falls back to the abstract). |
| `read_fulltext` | Full text; give a `query` for the most relevant passage, or page through it. |
| `related_papers` | Nearest neighbours by shared concepts. |
| `get_paper_citation` | BibTeX or CSL-JSON entry. |
| `get_paper_notes`, `get_paper_highlights` | Your own notes and highlights on a paper. |
| `global_search` | One keyword across papers, concepts, ideas, experiments, manuscripts, tasks. |

### Concepts and graph

`get_concept` (definition, related concepts, citing papers) · `list_concepts` (the topic's concept
inventory) · `knowledge_graph` (paper/concept/author nodes and edges).

### Figures

`list_paper_figures` (figure metadata) · `get_paper_figure` and `get_paper_figures` (the images
themselves, as MCP image blocks — useful for slides) · `find_figures` (search figures by topic
across the corpus).

These return real image data and are the most context-expensive tools here; ask for them
deliberately.

### Outside the library

`external_search` (Semantic Scholar, falling back to OpenAlex) · `get_references` and
`get_citations` (what a paper cites and who cites it) · `lookup_paper` (metadata by DOI). These call
third-party APIs, so they are slower and subject to rate limits.

### The workspace

| Tool | What it gives you |
| --- | --- |
| `get_project_status` | The orientation call: paper/idea/experiment/manuscript counts, pending approvals, source libraries, running tasks, recent activity. Start here. |
| `list_tasks` | AI tasks in this topic (ingest, idea forging, review, experiment, writing), filterable by `status` (`running`/`paused`/`done`) and `kind`. |
| `get_task` | One task in detail: plan steps and their verdicts, `blocked_on` when it is waiting for a human decision, `last_error` when it failed, `artifacts` for what it produced. |
| `read_task_logs` | That task's terminal log, including full model output, filterable by level. |
| `list_gates` | Human approval checkpoints — idea promotion, compute budget, paper submission — with the task each one blocks. |

Approving a gate is deliberately **not** exposed: an agent can see that work is waiting on a
decision and tell you, but the decision stays with a person in the web app.

### Corpus

`list_libraries` (which direction libraries you can see, how many papers each holds, which are
linked to this topic) · `get_library` (its statement, keywords, anchors, sync cadence) ·
`search_daily_pool` (the upstream feed of new arXiv announcements not yet collected into any
library).

A topic holds no papers of its own — its corpus is the union of the libraries linked to it. That is
why `get_project_status` lists `source_libraries`, and why an empty corpus usually means no library
is linked yet.

### Ideas and experiments

`list_ideas` and `get_idea` (candidates, scores, Elo rating, the full research proposal) ·
`list_experiments` and `get_experiment` (hypotheses, plan, per-round metrics, report, figures) ·
`read_experiment_logs` (the training log tail of a given round).

### Writing

`list_manuscripts` (title, template, status, review outcome, last compile) · `get_manuscript` (file
tree, compile entry point and engine, compile diagnostics) · `read_manuscript_file` (a file's
contents by path, paged) · `get_fact_pack` (the hypotheses, metrics, figures, and citations a
manuscript is allowed to draw on — the antidote to invented numbers).

### Assistant workflow

These exist primarily for PolarisBuddy, the in-app assistant that shares this tool layer, but they
appear in the MCP catalog too: `run_subagent` (delegate a retrieval-heavy sub-task to a fresh agent
that only reports its conclusion) · `skill_load` and `skill_read_file` (fetch an agent skill's body
and attachments on demand) · `recall` (search your own PolarisBuddy memory; its writing counterpart
`remember` is not exposed over MCP) · `submit_plan` (hand a step plan over for approval — only
meaningful in a conversation that can render it).

---

## 3. Recipes

**Orient yourself in an unfamiliar topic**

```
get_project_status                  → counts, source libraries, what is running
list_libraries linked_only=true     → where the corpus comes from
list_concepts                       → the vocabulary of this topic
search_papers query="…" k=5         → the papers that matter
```

**Answer a question from the literature, with citations**

```
search_chunks query="…"             → land on the exact paragraphs
read_wiki paper_id=…                → the compiled reading note
read_fulltext paper_id=… query="…"  → verify the claim in the source
get_paper_citation paper_id=…       → the BibTeX entry for what you write
```

**Find out why nothing is progressing**

```
list_tasks status="paused"          → what is stuck
get_task task_id=…                  → blocked_on tells you which gate, last_error why it failed
read_task_logs task_id=… level="error"
list_gates                          → then go approve it in the web app
```

**Write about an experiment without making numbers up**

```
list_experiments → get_experiment experiment_id=…   → real metrics and figures
read_experiment_logs experiment_id=…                → what actually happened in the run
get_fact_pack manuscript_id=…                       → the sanctioned facts for this paper
```

---

## 4. Behaviour and limits

- **Read-only.** Every tool is a query. `tools/list` reports `read_only: true` for all of them.
- **Truncation is explicit.** Long text is capped (wiki 8 000 chars, full text 6 000 per page, chunks
  1 200, logs by line count) and the response says so — `truncated: true`, or `page`/`pages` so you
  know a next call exists.
- **Errors are messages, not crashes.** A bad id or a missing precondition comes back as an MCP tool
  error with a Chinese explanation of what to do instead; it does not fail the connection.
- **Timeout** is 60 seconds per call.
- **Semantic search degrades.** If the embedding service is unreachable, semantic modes fall back to
  keyword search and say so in the response's `mode` field — results get worse, nothing breaks.
- **Images** come back as MCP image blocks, at most 4 per call.

---

## 5. Checking the connection

**Settings → MCP** in the web app is the control room:

- **Tool catalog** — every tool, its arguments, and whether it reaches the network.
- **Self-check** — pick a topic and run the whole catalog against its real data. Each tool comes
  back `OK`, `broken`, or `not tested` (no sample data of that kind in the topic, or a network tool
  you did not opt into). Run this first when an agent reports that something does not work.
- **Playground** — pick one tool, fill arguments in a form or as raw JSON, run it, and see exactly
  what a client receives, including the JSON-RPC request you can copy into your own client.

The same self-check runs in CI (`tests/test_mcp_server.py::test_selfcheck`), so a backend refactor
that breaks a tool fails the build rather than surfacing in your agent.

---

## 6. Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| `项目不存在或无权访问` | Wrong `project_id`, or you are not a member of that topic. Copy the id from `/t/<id>`. |
| `该任务不属于本课题` / `本课题内不存在该稿件` | The id belongs to another topic. MCP sessions are scoped to one topic at a time. |
| `文献库不存在或无权访问` | Someone else's personal library. Only your own and shared libraries are visible. |
| Empty corpus, no papers found | No library is linked to the topic yet — check `get_project_status`'s `source_libraries`. |
| `mode` comes back `keyword` when you asked for `semantic` | The embedding service is down; results are degraded but usable. |
| Tools missing from `tools/list` | Old server version, or the client cached an earlier list — reconnect. |

---

## See also

- [Core Concepts](concepts.md#the-mcp-read-only-tool-layer) — how the tool layer relates to the
  platform's internal agent.
- [Development](development.md#the-mcp-tools-during-development) — adding a tool, and the testing
  contract.
- [Configuration](configuration.md#mcp-stdio-variable) — environment variables for the stdio server.
