"""Transport-agnostic MCP tool visibility policies.

Runtime-specific profiles live in ``app.integrations``.  The MCP core only
knows how to apply a resolved policy, so adding an external runtime does not
add runtime-specific names or tool lists to the protocol implementation.
"""

from dataclasses import dataclass

from app.tools.registry import ToolSpec, list_tools

_CONVERSATION_ONLY = frozenset({"update_plan"})


@dataclass(frozen=True, slots=True)
class MCPToolProfile:
    """An immutable allow policy applied to both discovery and invocation."""

    name: str
    include_writes: bool
    excluded: frozenset[str]

    def exposes(self, spec: ToolSpec) -> bool:
        if spec.name in self.excluded:
            return False
        return spec.read_only or self.include_writes


DEFAULT_PROFILE = MCPToolProfile(
    name="default",
    include_writes=False,
    excluded=_CONVERSATION_ONLY,
)


def profile_tools(profile: MCPToolProfile) -> list[ToolSpec]:
    """Return the registry entries visible under one profile."""

    return [spec for spec in list_tools() if profile.exposes(spec)]
