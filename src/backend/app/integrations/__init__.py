"""Composition root for optional external-runtime adapters."""

from app.integrations.deepseek_harness.profile import PROFILES as DEEPSEEK_HARNESS_PROFILES
from app.mcp.profiles import DEFAULT_PROFILE, MCPToolProfile

_MCP_PROFILES = {profile.name: profile for profile in DEEPSEEK_HARNESS_PROFILES}


def resolve_mcp_profile(name: str | None) -> MCPToolProfile:
    """Resolve a transport header without coupling MCP core to one runtime."""

    if name is None or not name.strip():
        return DEFAULT_PROFILE
    normalized = name.strip()
    try:
        return _MCP_PROFILES[normalized]
    except KeyError as exc:
        raise ValueError(
            f"未知 MCP 工具 Profile：{normalized}；可用：{', '.join(sorted(_MCP_PROFILES))}"
        ) from exc
