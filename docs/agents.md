# Agent backends: bring your own Claude Code, Codex or Gemini CLI

Polaris can hand a conversation to a coding agent you already use. The assistant
panel keeps working the same way — streamed text, thinking, plans and tool
cards — but the answer comes from the agent you picked instead of Polaris's own
model loop.

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

1. Install the agent and sign in **on the machine that runs Polaris** (your
   computer for the desktop app; the server for a self-hosted deployment). The
   stock Docker `api` image has no Node.js, so on a Docker deployment the agents
   have to be added to a derived image; the desktop app uses whatever is
   installed on your computer.
2. Open **Settings → Agent backends**. Installed agents are marked; pick one and
   click **Add**.
3. Click **Check connection**. Polaris starts the agent, completes the handshake and shows
   what it reported: name, version, whether it can resume sessions and whether
   it can use Polaris tools. If it fails, the error says what to do — usually an
   install command or a sign-in command.
4. In the assistant panel, choose the agent from the backend picker. The choice
   is remembered per conversation; switch back to **Polaris** at any time.

Only the platform owner can register agents, because registering one lets the
server run that command. Other accounts on the same server can use an agent
only if the owner marks it **shared** — it spends the owner's subscription.

## What the agent may do

Every agent has a permission policy. When the agent asks for permission
(to edit a file, run a command, …) Polaris answers on your behalf:

| Policy | Answer |
|---|---|
| **Refuse anything that needs permission** (default) | Refuses everything that needs permission. The agent can read and talk. |
| **Allow reading and searching only** | Allows reading, searching, fetching and thinking; refuses edits and commands. |
| **Allow automatically** | Allows each request once. The agent may edit files and run commands. |

Each conversation gets its own working directory under the data directory
(`<data_dir>/acp-workspaces/<user>/<conversation>`). When the agent reads or
writes files through Polaris, the path must stay inside that directory —
`..` and symbolic links cannot escape it — and writes additionally require the
**Allow automatically** policy. Commands the agent runs with its own tools are governed by
the agent's own sandbox, so keep **Allow automatically** for agents you trust.

The agent process does not inherit the server's environment. It gets the
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

- Answering permission requests interactively in the UI (policies decide for now).
- Using agents inside experiment code generation and repair.
- Running agents on remote hosts over SSH.
