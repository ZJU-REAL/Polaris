# Agent backends: bring your own Claude Code, Codex or Gemini CLI

Polaris can run on a coding agent you already use. Add the agent once and it
can answer every model call Polaris makes — library scoring, wiki compiles,
idea generation, experiment planning, writing, review — as well as assistant
conversations. You don't need a model API key at all: the agent uses your own
sign-in or subscription.

Two ways to use an agent:

- **As the model.** With no model API configured, the agent answers every model
  call automatically. You can also point individual stages at an agent in the
  routing table, or make an agent the default model.
- **As the assistant.** In the assistant panel the agent takes over the whole
  conversation: streamed text, thinking, plans and tool cards, with Polaris's
  own tools available to it.

This works through the [Agent Client Protocol](https://agentclientprotocol.com)
(ACP): Polaris starts the agent as a local process and talks to it over
stdin/stdout, the same way an editor such as Zed does. The agent runs with your
own sign-in or subscription. Polaris never sees its keys.

> This is the opposite direction of [MCP](./mcp.md). With MCP, an external agent
> calls into Polaris. With agent backends, Polaris calls out to the agent — and,
> when the agent supports it, lends it Polaris's MCP tools for the session.

## Supported agents

| Agent | Command Polaris runs | Install | Sign in |
|---|---|---|---|
| Claude Code | `claude-agent-acp` | `npm install -g @agentclientprotocol/claude-agent-acp` (plus Claude Code itself) | `claude auth login` |
| Codex | `codex-acp` | `npm install -g @agentclientprotocol/codex-acp` (plus Codex itself) | `codex login` |
| Gemini CLI | `gemini --acp` | `npm install -g @google/gemini-cli` | run `gemini` once |
| Qwen Code | `qwen --acp` | `npm install -g @qwen-code/qwen-code` | run `qwen` once |
| OpenCode | `opencode acp` | `npm install -g opencode-ai` | `opencode auth login` |
| Kimi CLI | `kimi acp` | `uv tool install kimi-cli` | run `kimi` once |

Any other program that speaks ACP over stdio can be added as a **custom agent**
with its own command, arguments and environment variables.

## Set it up

1. Install the agent and sign in **on the same computer as the Polaris app**.
   The app's engine uses whatever is installed there.
2. Open **Settings → Models & agents** and go to **Agent backends**. Installed agents are marked; pick one and
   click **Add**.
3. Click **Check connection**. Polaris starts the agent, completes the handshake and shows
   what it reported: name, version, whether it can resume sessions and whether
   it can use Polaris tools. If it fails, the error says what to do — usually an
   install command or a sign-in command.
4. In the assistant panel, choose the agent from the backend picker. The choice
   is remembered per conversation; switch back to **Polaris** at any time.

Registering an agent lets Polaris run that command on your computer, so only
add agents you trust.

## The agent as Polaris's model

Settings → **Models & agents** shows, at the top, who answers model calls right
now. The rules:

1. A stage with its own route uses that route. A route can point at a model API
   or at an agent.
2. Otherwise it follows the `default` route, which can also point at an agent.
   Use **Use as default model** on an agent to set it.
3. With no `default` route at all, the first enabled agent answers.

Every model call runs in a fresh agent session, so earlier calls never colour
later ones. The session:

- runs in an empty scratch folder;
- refuses every permission request and gets no Polaris tools;
- is told to answer directly in the requested format.

Prompts that carry figures send them as images when the agent can see images
(Claude Code can); otherwise the text is used alone.

Differences from a model API:

- **Embeddings and rerank** can't come from an agent. Without a model API for
  them, search falls back to keyword matching and reranking to plain scores.
  Everything else works.
- **Speed.** Each call takes several seconds — the agent thinks before it
  answers — so large batch jobs such as scoring a whole library take longer.
  Up to four calls per agent run at once (`POLARIS_ACP_LLM_CONCURRENCY`).
- **Temperature, token limits and reasoning effort** have no equivalent over
  ACP and are ignored. A route's model name is passed to the agent when it
  offers model selection.
- **Token usage** is estimated from the text length.
- An experiment's `eval_model` option, which gives experiment code on another
  machine its own model access, needs a model API.

The engine keeps a small pool of agent processes for model calls, so the
agent must stay installed and signed in for your user account.

## What the agent may do

Every agent has a permission policy. When the agent asks for permission
(to edit a file, run a command, …) Polaris answers on your behalf:

| Policy | Answer |
|---|---|
| **Refuse anything that needs permission** | Refuses everything that needs permission. The agent can read and talk. |
| **Ask me each time** (default for new agents) | Shows the request in the assistant panel — what the agent wants to do and its options (allow once, always allow, reject). Nobody answers within five minutes, the conversation is closed or the turn is stopped → refused. |
| **Allow reading and searching only** | Allows reading, searching, fetching and thinking; refuses edits and commands. |
| **Allow automatically** | Allows each request once. The agent may edit files and run commands. |

With **Ask me each time**, a request is answered only by the person who owns the
conversation, and only while the agent is still waiting for it. Unattended, the
policy behaves exactly like **Refuse anything that needs permission**.

Each conversation gets its own working directory under the data directory
(`<data_dir>/acp-workspaces/<user>/<conversation>`). When the agent reads or
writes files through Polaris, the path must stay inside that directory —
`..` and symbolic links cannot escape it — and writes additionally require the
**Allow automatically** policy, or an edit you approved under **Ask me each time**
(one approval allows one write; "always allow" covers the rest of the session). Commands the agent runs with its own tools are governed by
the agent's own sandbox, so keep **Allow automatically** for agents you trust.

The agent process does not inherit the engine's environment. It gets the
basics it needs (`PATH`, `HOME`, locale, proxy settings) plus the variables you
set on the agent — never database URLs, encryption keys or provider API keys.

## Polaris tools inside the agent

When the agent supports HTTP MCP (Claude Code and Codex do), each session gets
Polaris's own `/mcp` endpoint with a token that is **read-only**, belongs to the
user who is chatting, expires after a day and is revoked when the session
closes. The agent can then search that user's libraries and read papers, wiki
pages and concepts with the same tools described in [MCP](./mcp.md) — and it
sees only what that user can see.

## Sessions

One agent process stays open per conversation and is closed after ten idle
minutes. If the process is gone and the agent supports resuming sessions,
Polaris continues the agent's own session; otherwise it starts a new one and
includes a short summary of the earlier conversation in the next prompt.

## Not yet supported

- Using agents inside experiment code generation and repair.
- Running agents on remote hosts over SSH.
