"""内置的 agent 模板：常见的说 ACP 的 CLI 怎么拉起、怎么装、怎么登录（#836）。

模板只是「预填表单」：登记时从模板拷出命令和参数，之后用户随时可以改。所以这里
写错一个参数不会把谁锁死，但要尽量和上游当前版本一致——2026-10 核对过：

- Claude Code / Codex 本身不说 ACP，要经官方适配器（npm ``@agentclientprotocol/*``），
  适配器再去调本机已登录的 ``claude`` / ``codex``。
- Gemini CLI、Qwen Code 用 ``--acp``（旧的 ``--experimental-acp`` 已标记弃用）。
- OpenCode、Kimi CLI 自带 ``acp`` 子命令。

``detect`` 是「装没装」的判据：适配器类的模板要求适配器和它背后的 CLI 都在。
"""

from __future__ import annotations

import os
import shutil
from dataclasses import asdict, dataclass

from app.services.acp.client import augmented_path


@dataclass(frozen=True, slots=True)
class AgentTemplate:
    id: str
    name: str
    description: str
    command: str
    args: tuple[str, ...] = ()
    #: 这些命令都能在 PATH 里找到，才算「已安装」
    detect: tuple[str, ...] = ()
    install: str = ""
    login: str = ""
    homepage: str = ""

    def as_dict(self) -> dict[str, object]:
        out = asdict(self)
        out["args"] = list(self.args)
        out["detect"] = list(self.detect)
        return out


TEMPLATES: tuple[AgentTemplate, ...] = (
    AgentTemplate(
        id="claude-code",
        name="Claude Code",
        description="Anthropic's coding agent, through the official ACP adapter.",
        command="claude-agent-acp",
        detect=("claude-agent-acp", "claude"),
        install="npm install -g @agentclientprotocol/claude-agent-acp",
        login="claude auth login",
        homepage="https://github.com/agentclientprotocol/claude-agent-acp",
    ),
    AgentTemplate(
        id="codex",
        name="Codex",
        description="OpenAI's coding agent, through the official ACP adapter.",
        command="codex-acp",
        detect=("codex-acp", "codex"),
        install="npm install -g @agentclientprotocol/codex-acp",
        login="codex login",
        homepage="https://github.com/agentclientprotocol/codex-acp",
    ),
    AgentTemplate(
        id="gemini",
        name="Gemini CLI",
        description="Google's open-source agent; speaks ACP natively.",
        command="gemini",
        args=("--acp",),
        detect=("gemini",),
        install="npm install -g @google/gemini-cli",
        login="gemini",
        homepage="https://github.com/google-gemini/gemini-cli",
    ),
    AgentTemplate(
        id="qwen",
        name="Qwen Code",
        description="Qwen's coding agent (a Gemini CLI fork); speaks ACP natively.",
        command="qwen",
        args=("--acp",),
        detect=("qwen",),
        install="npm install -g @qwen-code/qwen-code",
        login="qwen",
        homepage="https://github.com/QwenLM/qwen-code",
    ),
    AgentTemplate(
        id="opencode",
        name="OpenCode",
        description="Open-source agent that works with many model providers.",
        command="opencode",
        args=("acp",),
        detect=("opencode",),
        install="npm install -g opencode-ai",
        login="opencode auth login",
        homepage="https://opencode.ai",
    ),
    AgentTemplate(
        id="kimi",
        name="Kimi CLI",
        description="Moonshot's command-line agent; speaks ACP natively.",
        command="kimi",
        args=("acp",),
        detect=("kimi",),
        install="uv tool install kimi-cli",
        login="kimi",
        homepage="https://github.com/MoonshotAI/kimi-cli",
    ),
)

_BY_ID = {t.id: t for t in TEMPLATES}

#: 自定义：不出现在模板列表里，只是登记时 template 字段的取值
CUSTOM = "custom"


def get_template(template_id: str) -> AgentTemplate | None:
    return _BY_ID.get(template_id)


def which(command: str) -> str | None:
    """和拉起 agent 时同一份 PATH（补过常见安装目录）去找命令。"""
    if os.path.isabs(command):
        return command if os.access(command, os.X_OK) else None
    return shutil.which(command, path=augmented_path(os.environ.get("PATH", "")))


def detect(template: AgentTemplate) -> dict[str, object]:
    """{"installed": bool, "found": {cmd: path|None}}——前端据此显示「已安装 / 缺什么」。"""
    found = {cmd: which(cmd) for cmd in template.detect or (template.command,)}
    return {"installed": all(found.values()), "found": found}


__all__ = ["CUSTOM", "TEMPLATES", "AgentTemplate", "detect", "get_template", "which"]
