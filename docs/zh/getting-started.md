# 快速上手

Polaris 是一个桌面应用。所有东西都在你自己的电脑上运行——一个本地引擎加一个 SQLite 数据库——所以不用架服务器、不用注册账号，首次启动前也什么都不用配。这篇指南带你从安装应用一路走到建好第一个文献库。想参与开发 Polaris 本身，见 [Development](../../docs/development.md)（英文）；引擎读取的环境变量见 [Configuration](../../docs/configuration.md)（英文）。

## 1. 安装应用

到 [Releases](https://github.com/ZJU-REAL/Polaris/releases/latest) 下载对应平台的安装包：

| 平台 | 文件 |
| --- | --- |
| macOS（universal） | `.dmg` 或 `.zip` |
| Windows | `.exe` 安装包或便携版 `.zip` |
| Linux | `.AppImage` 或 `.deb` |

这些构建**既未签名也未公证**，所以每个平台都要先告诉系统一次它是安全的：

- **macOS**：执行 `xattr -dr com.apple.quarantine /Applications/Polaris.app`，或右键应用选「打开」。
- **Windows**：在 SmartScreen 提示里选「更多信息 → 仍要运行」（便携版 zip 不会弹这个提示）。
- **Linux**：AppImage 需要 `libnss3 libgtk-3-0 libasound2`；在 Ubuntu 24.04+ 的 AppArmor 限制下要加 `--no-sandbox` 启动。

> [!NOTE]
> v0.3.9 及更早的版本只是连接远程服务器的外壳，一定会要求填服务器地址，也没法自己更新成本地版。请下载当前版本直接覆盖安装。

## 2. 首次启动

首次启动时应用会装好自己的引擎：把 Python 工具链和引擎依赖下载到它自己的数据目录里，然后启动引擎、打开主窗口。进度页会显示每一步。这可能需要几分钟；之后启动会跳过这一步，直接打开。

不需要登录：你是唯一的用户，应用里的一切都归你。你的数据——SQLite 数据库、PDF、导出文件、实验日志——都在应用的用户数据目录里，见 [Where the data lives](../../docs/desktop.md#where-the-data-lives)（英文）。

应用会检查更新（设置 → 关于），能不重装就直接应用。

## 3. 给 Polaris 配一个模型

打开 **设置 → 模型与智能体**（`/settings?tab=llm`）。在有可用模型之前，AI 功能会返回 `LLM_NOT_CONFIGURED`。

- **最省事：智能体后端。** 如果你已经在用 Claude Code、Codex 或 Gemini CLI，在同一台电脑上装好并登录，然后在 **智能体后端** 里添加它，点 **检查连接**。没配模型 API 时，第一个启用的智能体会回答所有模型调用——不需要 API key。见 [Agent backends](../../docs/agents.md)（英文）。
- **可选：模型 API。** 添加一个提供商（OpenAI 兼容、OpenAI Responses 或 Anthropic）并填入密钥，再在路由表里把各科研阶段映射过去。向量嵌入和重排只能来自模型 API；没有的话，检索会退回关键词匹配。

<!-- screenshot: Settings → Models & agents, the model routing table -->

## 4. 在应用里接着做

按顺序来，每一步都为下一步铺路。

1. **连接一台 SSH 服务器**（实验阶段需要）——**设置 → SSH 凭据**（`/settings?tab=ssh`）。添加主机和密钥，然后点 **测试连接**；凭据会加密保存。实验策略（命令白名单/黑名单、预算）在 **设置 → 实验** 里。
2. **建第一个方向文献库**——打开 **文献库**（`/libraries`）新建一个。一场结构化的 AI 访谈会帮你写出收录配置（方向陈述、目标、范围、排除项），运行摄入就会构建语料：候选检索、引用滚雪球、相关性打分、全文抽取和 wiki 编译，整个过程是一个可恢复的任务。
3. **建课题并关联文献库**——新建一个课题（`/projects/new`），再把一个或多个方向文献库关联上去。课题本身不存论文，它的语料就是所关联文献库的并集。之后按流水线一阶段一阶段往下走——见 [Core concepts](../../docs/concepts.md)（英文）。
4. **可选：配置每日新论文**——**设置 → 每日论文** 里设置订阅的分类和每天抓取的时间。引擎每隔几分钟检查一次是否到点，所以只有应用开着的时候才会更新。
5. **可选：打开 PolarisBuddy 的工具循环**——应用内助手的多轮工具循环默认关闭（它每一轮都要重发历史和工具 schema，比一次性对话贵得多）。开关是环境变量 `POLARIS_CHAT_AGENT_ENABLED=1`；引擎会继承应用的环境变量，所以在终端里带上这个变量启动应用即可。

> [!WARNING]
> 实验模块会通过 SSH 连接真实的 GPU 服务器并在上面运行生成的代码。每条远程命令都来自固定的模板白名单并写入审计日志，创建实验时也可以要求显式的算力预算审批——但你仍然应该只把 Polaris 指向你自己控制的机器，并定期查看审计日志。

## 常见首跑问题

| 现象 | 原因与解决 |
| --- | --- |
| 进度页停在 `python` 或 `install` | 首次启动要下载 Python 工具链和依赖包。检查网络（或代理）后重启应用，安装会干净地从头再来。 |
| macOS 提示应用「已损坏」或无法打开 | 未签名构建带着隔离标记。执行 `xattr -dr com.apple.quarantine /Applications/Polaris.app`。 |
| AI 功能返回 `LLM_NOT_CONFIGURED`（HTTP 503） | 没有启用的智能体后端，也没有可用的模型 API 路由。到 **设置 → 模型与智能体** 添加一个智能体或模型 API。 |
| 智能体后端 **检查连接** 失败 | 这台电脑上没装这个智能体，或者没登录。错误信息会写明要执行的安装或登录命令。 |
| 文献摄入什么都找不到 / arXiv、Semantic Scholar 或 OpenAlex 超时 | 你的网络直连文献 API 被挡或不稳定。为文献 API 设置出站代理（见 [Configuration](../../docs/configuration.md)，英文）。 |
| LaTeX 编译提示编译器未安装 | 稿件用你电脑上装的 `tectonic`（或经 `latexmk` 调用的 TeX Live）编译。装好其中一个，并确认它在 `PATH` 里。 |

## 下一步

- [Introduction](../../docs/index.md)（英文）：每个流水线阶段做什么，并链接到各阶段的使用指南。
- [Core concepts](../../docs/concepts.md)（英文）：流水线、Voyage、技能与 MCP 工具。
- [Agent backends](../../docs/agents.md)（英文）：把 Claude Code 等智能体当作 Polaris 的模型。
- [Desktop app](../../docs/desktop.md)（英文）：应用如何启动引擎，数据放在哪里。
- [Development](../../docs/development.md)（英文）：从源码运行 Polaris、迁移、测试与 Git 约定。
