"""内置的智能体模板：常见的说 ACP 的 CLI 怎么拉起、怎么装、怎么登录（#836）。

模板只是「预填表单」：登记时从模板拷出命令和参数，之后用户随时可以改。所以这里
写错一个参数不会把谁锁死，但要尽量和上游当前版本一致——2026-11 核对过：

- Claude Code / Codex 本身不说 ACP，要经官方适配器（npm ``@agentclientprotocol/*``）。
  两个适配器都自带一份对应的 CLI（Claude Agent SDK / ``@openai/codex``），用的是本机
  已登录的账号（``~/.claude``、``~/.codex``），所以「装没装」只看适配器。
- pi（``@earendil-works/pi-coding-agent``，原 ``@mariozechner/pi-coding-agent``）本身
  只有 ``--mode rpc``；ACP 经社区适配器 ``pi-acp``（Zed 的 ACP 目录里登记的就是它），
  适配器再拉起本机的 ``pi``。它**不在动手前征求同意**、不把文件读写交给我们、也不接
  MCP——权限策略对它不起作用，界面上要说清楚（``permission_prompts=False``）。
- Gemini CLI、Qwen Code 用 ``--acp``（旧的 ``--experimental-acp`` 已弃用）。
- OpenCode、Kimi CLI 自带 ``acp`` 子命令。

``detect`` 是「装没装」的判据：这些命令都在 PATH 里才算装好了。

``env_passthrough``：这个智能体自己认的凭据/配置变量。拉起它时，引擎环境里有的这几个
名字原样带过去（别的一概不带，见 client.inherited_env）——这样在终端里靠 shell 变量
登录的智能体，从 Polaris 里拉起也一样能用。**不带 ANTHROPIC_***：那是 Polaris 自己的
key，交给 Claude Code 会让它绕过用户自己的登录、改用这把 key 计费。
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
    #: 智能体会不会在写文件、跑命令之前征求同意（ACP session/request_permission）。
    #: 不会的，权限策略对它不起作用。
    permission_prompts: bool = True
    #: 拉起时从引擎环境里带过去的变量名（它自己的凭据与配置）
    env_passthrough: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        out = asdict(self)
        out["args"] = list(self.args)
        out["detect"] = list(self.detect)
        out["env_passthrough"] = list(self.env_passthrough)
        return out


TEMPLATES: tuple[AgentTemplate, ...] = (
    AgentTemplate(
        id="claude-code",
        name="Claude Code",
        description="Anthropic's coding agent, through the official ACP adapter.",
        command="claude-agent-acp",
        detect=("claude-agent-acp",),
        install="npm install -g @agentclientprotocol/claude-agent-acp",
        login="claude auth login",
        homepage="https://github.com/agentclientprotocol/claude-agent-acp",
        env_passthrough=("CLAUDE_CONFIG_DIR",),
    ),
    AgentTemplate(
        id="codex",
        name="Codex",
        description="OpenAI's coding agent, through the official ACP adapter.",
        command="codex-acp",
        detect=("codex-acp",),
        install="npm install -g @agentclientprotocol/codex-acp",
        login="codex login",
        homepage="https://github.com/agentclientprotocol/codex-acp",
        env_passthrough=("CODEX_HOME", "CODEX_API_KEY", "OPENAI_API_KEY", "CODEX_PATH"),
    ),
    AgentTemplate(
        id="pi",
        name="pi",
        description="A minimal open-source coding agent, through the pi-acp adapter.",
        command="pi-acp",
        detect=("pi-acp", "pi"),
        install="npm install -g @earendil-works/pi-coding-agent pi-acp",
        login="pi-acp --terminal-login",
        homepage="https://github.com/svkozak/pi-acp",
        permission_prompts=False,
        env_passthrough=(
            "OPENAI_API_KEY",
            "GEMINI_API_KEY",
            "OPENROUTER_API_KEY",
            "GROQ_API_KEY",
            "XAI_API_KEY",
            "MISTRAL_API_KEY",
        ),
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
        env_passthrough=(
            "GEMINI_API_KEY",
            "GOOGLE_API_KEY",
            "GOOGLE_CLOUD_PROJECT",
            "GOOGLE_CLOUD_LOCATION",
            "GOOGLE_GENAI_USE_VERTEXAI",
            "GOOGLE_APPLICATION_CREDENTIALS",
        ),
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
        env_passthrough=("DASHSCOPE_API_KEY", "OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL"),
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
        env_passthrough=("OPENCODE_CONFIG", "OPENAI_API_KEY", "OPENROUTER_API_KEY"),
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
        env_passthrough=("KIMI_API_KEY", "MOONSHOT_API_KEY"),
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
