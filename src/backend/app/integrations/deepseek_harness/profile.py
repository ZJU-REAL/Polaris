"""DeepSeek Harness-specific MCP profile declarations."""

from app.mcp.profiles import MCPToolProfile

_NATIVE_REPLACEMENTS = frozenset(
    {
        "run_subagent",
        "skill_load",
        "skill_read_file",
        "submit_plan",
        "update_plan",
    }
)

READONLY_PROFILE = MCPToolProfile(
    name="dsh-readonly-v1",
    include_writes=False,
    excluded=_NATIVE_REPLACEMENTS,
)
FULL_PROFILE = MCPToolProfile(
    name="dsh-full-v1",
    include_writes=True,
    excluded=_NATIVE_REPLACEMENTS,
)

PROFILES = (READONLY_PROFILE, FULL_PROFILE)
