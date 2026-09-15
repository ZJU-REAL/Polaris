"""DeepSeek Harness 侧的 MCP 工具 Profile 声明。

技能功能移除后，原 ``integrations/deepseek_harness/`` 那个包整体删掉了——它的
service/api/schemas 全部是「把 Polaris 的助手技能只读投影出去」，技能没了就没有
内容可投。

但 Profile 不是技能的一部分：它说的是「这个外部运行时自带哪些原生工具，所以
Polaris 不要重复暴露」。排除清单里 ``submit_plan`` / ``update_plan`` /
``run_subagent`` 与技能无关，而 ``app/mcp/http.py`` 经 ``resolve_mcp_profile``
真的在用它。所以它留下来，只是从被删的包里搬到这一层。
"""

from app.mcp.profiles import MCPToolProfile

#: 该运行时自带、不需要 Polaris 再暴露一份的工具。
#: skill_load / skill_read_file 是技能时代的原生工具名，对方仍可能带着——
#: 留在排除清单里无害（Polaris 这边已经没有同名工具），删掉反而要求对方同步升级。
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
