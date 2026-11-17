# Agent backends (removed)

Agent backends are no longer available. Polaris used to be able to run on a coding agent such as
Claude Code, Codex, or Gemini CLI over the Agent Client Protocol (ACP), either as its model or in
the assistant panel. That feature has been removed, together with the assistant's backend picker
and the permission cards for external agents.

What applies now:

- **Polaris uses Claude directly.** Every model call goes to the Claude API with one fixed model.
  The assistant (PolarisBuddy) always runs Polaris's own tool loop on Claude.
- **Set the key with an environment variable.** Put `export ANTHROPIC_API_KEY=sk-ant-...` (or the
  `LITELLM_API_KEY` / `LITELLM_API_BASE` pair for a gateway) in `~/.zshrc` or `~/.bashrc`, restart
  Polaris, and check under **Settings → Model**. The full list of variables and the order they are
  read in is in [Configuration](configuration.md#model).
- **Connecting external agents to Polaris is unaffected.** Claude Code, Codex, Cursor, and other
  clients can still read your research workspace through Polaris's MCP server. See [MCP](mcp.md).
