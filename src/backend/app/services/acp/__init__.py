"""外部 agent 后端：Polaris 作为 ACP client 驱动 Claude Code、Codex、Gemini CLI 等（#836）。

- connection：子进程 + 按行 JSON-RPC
- client：握手、会话、一轮对话、权限策略、限定在工作目录里的文件读写
- templates：内置 agent 模板与安装探测
- registry：登记表行 → AgentSpec；探测
- pool：对话 ↔ 常驻 agent 进程
"""
