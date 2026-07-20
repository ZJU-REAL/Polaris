# API：MCP 只读工具服务

把 Polaris 的检索能力（概念 / 文献 / 知识 / 项目状态）统一成一批**只读工具**，
同时供两类消费者使用：

- **内部**：Polaris 自己的 voyage agent 在写作 / 评审 / 想法构建 / 实验 / 文献分析中，
  用 `agents/voyage/tool_loop.run_tool_loop` 让 LLM 在生成中途按需检索（而非把封顶上下文一次塞进 prompt）。
- **外部**：一个 **MCP 协议服务器**，把同一批工具暴露给外部 MCP 客户端（Claude Desktop / Cursor 等）。

工具定义是**单一事实源**：全部在 `backend/app/tools/*` 用 `@tool` 注册，复用既有 `services/*`，
按项目隔离、只读（不含任何写 / 删 / SSH）。新增工具只需在 `app/tools/` 加一个 handler，
内部循环与外部 MCP 同时可见。

## 架构

```
app/tools/                     统一只读工具注册表（ToolSpec：name + description + JSON Schema + handler）
  ├─ literature.py             search_papers / read_wiki / read_fulltext / get_concept / list_concepts
  ├─ knowledge.py              search_chunks / get_paper / knowledge_graph / global_search
  ├─ project_state.py          list_ideas / get_idea / list_experiments / get_experiment / get_fact_pack
  └─ external.py               external_search / get_references / get_citations / lookup_paper
        │                                   │
   内部消费                              外部消费
   agents/voyage/tool_loop.py         app/mcp/
   run_tool_loop(ctx, tool_names=…)   dispatch.py（JSON-RPC 核心）
   （LLM 输出 {"tool":…}/{"finish":…}）  ├─ http.py  → POST /mcp（Streamable HTTP）
                                       └─ __main__.py → python -m app.mcp（stdio）
```

`ToolContext`（`app/tools/context.py`）把工具从 voyage 的 `ActionContext` 解耦，只带
`project_id / user_id / voyage_id / llm`，因此内部和外部两条路径都能构造。

## 工具目录（26 个，全部只读）

图片类工具（`get_paper_figure` / `get_paper_figures`）返回 **MCP image content block**
（inline base64 PNG，按 `max_dim` 缩放，默认单边 1600px）——外部客户端可直接拿到「方法图 /
实验图」做 PPT；图片已在建库时抽取、按 `kind`（motivation/method/architecture/experiment/other）
分类、配好中文图注。

| 工具 | 来源 | 参数（**必填**） | 说明 |
|---|---|---|---|
| `list_paper_figures` | 库内 | **paper_id** | 列出某论文所有图的元数据（不含图片） |
| `get_paper_figure` | 库内(图) | **paper_id**, **index**, max_dim | 取某张图的图片(PNG)+图注 |
| `get_paper_figures` | 库内(图) | **paper_id**, kind, only_important, max_dim | 批量取图片（默认只重要图） |
| `find_figures` | 库内 | **query**, kind, k | 跨库按主题/类型找图（返回元数据） |
| `get_paper_citation` | 库内 | **paper_id**, format | 引用条目（bibtex/csl） |
| `get_paper_notes` | 库内 | **paper_id** | 项目笔记（团队共享） |
| `get_paper_highlights` | 库内 | **paper_id** | 划线/高亮（含页码与选中文本） |
| `related_papers` | 库内 | **paper_id**, k | 共享概念最多的近邻论文 |
| `search_papers` | 库内 | **query**, mode, k | 库内检索论文（语义，pgvector 不可用时降级关键词） |
| `search_chunks` | 库内 | **query**, k | 全文段落级语义检索（比 search_papers 更细） |
| `read_wiki` | 库内 | **paper_id** | 读某论文的 wiki 综述页 |
| `read_fulltext` | 库内 | **paper_id**, query, page | 读论文全文（有 query 返回最相关段落，否则分页） |
| `get_paper` | 库内 | **paper_id** | 论文元数据 + 概念标签 |
| `get_concept` | 库内 | **name** | 概念定义 + 相关概念 + 关联论文 |
| `list_concepts` | 库内 | category | 项目概念清单 |
| `knowledge_graph` | 库内 | — | 项目知识图谱（论文/概念/作者节点与边） |
| `global_search` | 库内 | **q** | 跨实体检索：论文/概念/想法/实验/稿件/任务 |
| `list_ideas` | 库内 | status, depth, research_type | 项目想法清单 |
| `get_idea` | 库内 | **idea_id** | 想法完整内容 + 结构化 goal |
| `list_experiments` | 库内 | — | 项目实验清单 |
| `get_experiment` | 库内 | **experiment_id** | 实验详情（假设/计划/运行与指标） |
| `get_fact_pack` | 库内 | **manuscript_id** | 稿件事实包（想法 + 实验指标/图表 + 引用） |
| `external_search` | 网络 | **query**, k | 库外文献检索（S2，失败降级 OpenAlex） |
| `get_references` | 网络 | paper_ref \| paper_id | 某论文引用的参考文献 |
| `get_citations` | 网络 | paper_ref \| paper_id | 引用某论文的后续工作 |
| `lookup_paper` | 网络 | **doi** | 按 DOI 查库外论文元数据 |

> `get_references / get_citations` 接受外部 id（`paper_ref`：`arXiv:xxx` / `DOI:xxx` / S2 id）
> 或库内 `paper_id`（自动解析成外部 id）。

**图片传输**：工具可返回富内容 `ToolResult{payload, images}`（`app/tools/registry.py`）；
MCP 层（`app/mcp/dispatch.py`）把 `payload` 转文本块、每张图转 `image` content block
（`{"type":"image","data":<base64>,"mimeType":"image/png"}`）。纯文本工具照旧返回 dict。

**平台内对话**：文献库对话（`library_chat`）会把命中论文的重要图元数据+图注带进上下文，
让 AI 在回答里插入 `[[fig:论文id:图号]]` 标记，前端渲染成内联配图（复用
`GET /papers/{id}/figures/{index}/image`）——AI 找到相关论文的图并就地说明。

## 传输一：Streamable HTTP（`POST /mcp`）

JSON-RPC 2.0，支持单条与批量。**认证**复用平台 JWT：先 `POST /api/auth/jwt/login`
拿到 token，再在每个 MCP 请求带 `Authorization: Bearer <token>`。**项目**由每个工具的
必填入参 `project_id` 指定，服务端校验该用户是否为项目成员（非成员一律当作项目不存在）。

握手与调用：

```bash
# 1) initialize
curl -s localhost:8000/mcp -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18"}}'

# 2) tools/list
curl -s localhost:8000/mcp -H "Authorization: Bearer $TOKEN" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/list"}'

# 3) tools/call（注意 arguments 里带 project_id）
curl -s localhost:8000/mcp -H "Authorization: Bearer $TOKEN" \
  -d '{"jsonrpc":"2.0","id":3,"method":"tools/call",
       "params":{"name":"search_papers",
                 "arguments":{"project_id":"<PROJECT_UUID>","query":"retrieval augmented"}}}'
```

`tools/call` 返回 `{"content":[{"type":"text","text":"<JSON 字符串>"}],"isError":false}`；
工具级错误（参数非法 / 越权 / 论文不存在）返回 `isError:true`，文本为错误说明（不作为 JSON-RPC error）。

支持 HTTP 的 MCP 客户端（如 Cursor）配置示例：

```json
{
  "mcpServers": {
    "polaris": {
      "url": "http://localhost:8000/mcp",
      "headers": { "Authorization": "Bearer <TOKEN>" }
    }
  }
}
```

## 传输二：stdio（`python -m app.mcp`）

给本地桌面客户端（如 Claude Desktop）。本地进程视为可信：用户由环境变量
`POLARIS_MCP_USER_EMAIL` 指定（须已注册）；每个工具调用仍在 `arguments` 里带 `project_id`。

Claude Desktop 配置示例：

```json
{
  "mcpServers": {
    "polaris": {
      "command": "python",
      "args": ["-m", "app.mcp"],
      "env": {
        "POLARIS_MCP_USER_EMAIL": "you@example.com",
        "DATABASE_URL": "postgresql+asyncpg://…",
        "POLARIS_*": "（同后端运行所需的环境变量）"
      }
    }
  }
}
```

## 实现说明

- 协议为手写的 JSON-RPC 2.0 核心（`app/mcp/dispatch.py`），**不依赖外部 MCP SDK**，
  覆盖 `initialize` / `notifications/initialized` / `ping` / `tools/list` / `tools/call`。
- HTTP 走 JSON 响应模式（未实现 SSE 长连推送）；请求/响应式调用完全可用。
- 只读硬约束：`ToolSpec.read_only=True`；MCP 只暴露注册表里的只读工具，没有任何写操作入口。

## 测试

- `backend/tests/test_tools_registry.py`：注册/派发/渲染 + 库内工具端到端 + 跨项目隔离。
- `backend/tests/test_mcp_server.py`：`POST /mcp` 的鉴权、initialize、tools/list、tools/call、越权拒绝。
- `backend/tests/test_idea_proposal.py`：想法构建仍走泛化后的 `run_tool_loop`（parity）。
