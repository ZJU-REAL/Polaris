# MCP: driving Polaris from Claude Code, Codex, or Cursor

Polaris exposes its retrieval and workspace surface as an [MCP](https://modelcontextprotocol.io)
server, so an external coding agent can read your research workspace directly: search the papers
you have collected, read their wiki pages and full text, look at concepts and the knowledge graph,
see which AI tasks are running, what is waiting on a human decision, and how far a manuscript has
got.

The default MCP surface is read-only. An agent connected with the ordinary
profile can inform itself about your work but cannot change it. A separately
authorized DeepSeek Harness profile can expose `remember`, which writes only to
the current user's assistant memory, and only once that user has turned Buddy
memory on — the same opt-in the in-app tool surface requires. Starting an
ingest, forging ideas, launching an experiment, approving a gate, and drafting a
paper remain web-only.

---

## 1. Connect

Both transports expose the legacy catalog by default. Streamable HTTP also
supports the scoped profiles described below.

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

Call `list_accessible_projects` first when the agent doesn't know which topic to
use. This user-scoped tool needs no `project_id`; it returns the IDs, names,
slugs, status, and statements of the topics available to the authenticated user.

Every project-scoped tool call carries a `project_id`, which identifies the
research topic you want the agent to inspect. The user-scoped `recall` and
`remember` tools derive identity from the credential and need no project ID.
The server verifies your access on every call. A task, manuscript, or library
belonging to another topic is reported as not found, even when you can access
it through another topic. Library visibility follows the platform's existing
rules: your own personal libraries plus every shared library, and nothing more.

You can also find a topic ID in the web app's URL: `/t/<topic-id>`.

---

### Long-lived integration tokens and profiles

Persistent agents should use a scoped integration token instead of a browser
JWT. Create one with `POST /api/integration-tokens`; the plaintext is returned
once, while Polaris stores only a digest. Tokens can carry `skills:read`,
`mcp:read`, and `mcp:write`, expire after a configured number of days, and can
be revoked with `DELETE /api/integration-tokens/{id}`.

The optional `X-Polaris-Tool-Profile` request header selects a stable catalog:

| Profile | Tools | Required scope |
| --- | --- | --- |
| Omitted | Legacy read-only catalog | `mcp:read` |
| `dsh-readonly-v1` | Read-only catalog without DSH-native duplicates | `mcp:read` |
| `dsh-full-v1` | DSH catalog plus approved write tools | `mcp:read`, `mcp:write` |

Profiles govern both discovery and direct invocation. A client cannot call a
hidden tool by guessing its name. See the
[DeepSeek Harness bundle](../integrations/deepseek-harness/README.md) for token,
installation, and skill-provider instructions.

---

## 2. Tools

The legacy catalog contains 46 tools in ten groups. The DSH profiles hide the
skill, planning, and sub-agent tools because Harness already provides those
natively. Names are stable within a versioned profile; treat them as API.

### Project discovery

`list_accessible_projects` is the bootstrap tool. It lists the topics available
to the current authenticated user and returns the `project_id` values required
by the rest of the catalog. It supports name or slug filtering, status
filtering, and pagination. The server derives the user identity from the bearer
token or `POLARIS_MCP_USER_EMAIL`; the caller never supplies a user ID.

### Papers and reading

| Tool | What it gives you |
| --- | --- |
| `scan_papers` | Browse or precisely filter the topic's papers without loading abstracts. Filter by library, author, affiliation, publication date, ingestion date, status, tags, or reading state, and sort and paginate the result. |
| `search_papers` | Search papers in the topic's corpus. `mode=semantic` (default) falls back to keyword when embeddings are unavailable. |
| `search_chunks` | Passage-level search — lands on the paragraph rather than the paper. |
| `grep_fulltext` | Literal string match across the full texts, with a small context window per hit. Better than semantic search for exact terms, model names, dataset names, or formula symbols. |
| `get_paper` | Metadata, authors, status, abstract, concept tags. `in_library` tells you whether the paper is actually collected into a library — `false` means it is still only a daily-pool candidate. |
| `read_wiki` | The platform's compiled reading note for a paper (falls back to the abstract). |
| `read_fulltext` | Full text; give a `query` for the most relevant passage, or page through it. |
| `related_papers` | Nearest neighbours by shared concepts. |
| `get_paper_citation` | BibTeX or CSL-JSON entry. |
| `get_paper_notes`, `get_paper_highlights` | Your own notes and highlights on a paper. |
| `global_search` | One keyword across papers, concepts, ideas, experiments, manuscripts, tasks. |

Use `scan_papers` when you need an inventory rather than a small relevance-ranked
answer. You can omit `query` to list papers. The date filters accept ISO 8601
dates or timestamps and include both boundaries:

```json
{
  "project_id": "<PROJECT_UUID>",
  "author": "Carol Zhang",
  "affiliation": "Zhejiang University",
  "published_from": "2024-01-01",
  "published_to": "2024-12-31",
  "sort": "-published_at",
  "page": 1,
  "limit": 30
}
```

The `sort` values are `relevance`, `published_at`, `-published_at`,
`created_at`, and `-created_at`. A leading minus sign means descending order.
`created_at` is the time the paper entered the library. The response includes
`total`, `has_more`, and `next_page`. Set `library_id` only to a library linked
to the selected topic; call `list_libraries` with `linked_only=true` to discover
those IDs.

For compatibility, a query-only call still supports `mode=semantic` and the old
`k` argument. When you combine `query` with exact filters or time sorting,
`scan_papers` uses deterministic title and abstract matching and reports
`mode=filtered`.

### Concepts and graph

`get_concept` (definition, related concepts, citing papers) · `list_concepts` (the topic's concept
inventory) · `knowledge_graph` (paper/concept/author nodes and edges).

### Figures

`list_paper_figures` returns figure metadata. `get_paper_figure` and
`get_paper_figures` return PNG download URLs and captions. `find_figures`
searches figures by topic across the corpus.

Figure download URLs are signed bearer links. They expire after 15 minutes by
default, and the download endpoint checks that the user who created the link
can still access the paper. The image bytes don't enter the MCP response or the
model context. HTTP figure links always use the same origin as the MCP endpoint:
for example, `https://polaris.example.edu/mcp` returns a link under
`https://polaris.example.edu/api/`. Download the URL when you need the actual
file.

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

Papers returned by `search_daily_pool` are, by definition, in no library at all, so the reading
tools accept their ids as a deliberate exception: `get_paper`, `read_wiki` and `read_fulltext` will
resolve a daily-pool paper even though it has no library membership, and report `in_library: false`.
Everything else stays scoped to the libraries this session can search.

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
`remember` is excluded from read-only profiles) · `submit_plan` (hand a step
plan over for approval — only meaningful in a conversation that can render it).
The `dsh-full-v1` profile exposes `remember` only when the integration token has
`mcp:write` and the account itself is not read-only.

---

## 3. Recipes

**Orient yourself in an unfamiliar topic**

```
list_accessible_projects              → choose a project_id when one is not already known
get_project_status                  → counts, source libraries, what is running
list_libraries linked_only=true     → where the corpus comes from
list_concepts                       → the vocabulary of this topic
scan_papers sort="-created_at"       → newest additions across the corpus
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

- **Read-only by default.** The legacy and `dsh-readonly-v1` catalogs contain
  only query tools. `dsh-full-v1` additionally exposes explicitly approved
  writes and requires a scoped credential.
- **Truncation is explicit.** Long text is capped (wiki 8 000 chars, full text 6 000 per page, chunks
  1 200, logs by line count) and the response says so — `truncated: true`, or `page`/`pages` so you
  know a next call exists.
- **Errors are messages, not crashes.** A bad id or a missing precondition comes back as an MCP tool
  error with a Chinese explanation of what to do instead; it does not fail the connection.
- **Timeout** is 60 seconds per call.
- **Semantic search degrades.** If the embedding service is unreachable, semantic modes fall back to
  keyword search and say so in the response's `mode` field — results get worse, nothing breaks.
- **Images use download links.** Figure tools return signed URLs instead of
  base64 MCP image blocks. A batch call returns at most eight links.

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
| `缺少或非法的 project_id` | Call `list_accessible_projects`, select the matching topic, and pass its `project_id` to the next tool. |
| `项目不存在或无权访问` | The `project_id` is wrong, or you can't access that topic. Call `list_accessible_projects` again to get the current accessible set. |
| `该任务不属于本课题` / `本课题内不存在该稿件` | The id belongs to another topic. MCP sessions are scoped to one topic at a time. |
| `文献库不存在或无权访问` | Someone else's personal library. Only your own and shared libraries are visible. |
| Empty corpus, no papers found | No library is linked to the topic yet — check `get_project_status`'s `source_libraries`. |
| `mode` comes back `keyword` when you asked for `semantic` | The embedding service is down; results are degraded but usable. |
| A figure download URL returns `FIGURE_LINK_INVALID` | The signed URL expired or was modified. Call `get_paper_figure` again to create a new link. |
| A stdio figure result contains a relative URL | Set `POLARIS_PUBLIC_BASE_URL` to the server root that the agent can reach. |
| Tools missing from `tools/list` | Old server version, or the client cached an earlier list — reconnect. |
| `MCP_WRITE_SCOPE_REQUIRED` | `dsh-full-v1` needs both `mcp:read` and `mcp:write`, and the account must not be read-only. |

---

## See also

- [Core Concepts](concepts.md#the-mcp-read-only-tool-layer) — how the tool layer relates to the
  platform's internal agent.
- [Development](development.md#the-mcp-tools-during-development) — adding a tool, and the testing
  contract.
- [Configuration](configuration.md#application-settings-polaris_-prefix) — public URL and link
  lifetime settings.
- [DeepSeek Harness bundle](../integrations/deepseek-harness/README.md) — native
  skills, MCP profiles, installation, and operations.
