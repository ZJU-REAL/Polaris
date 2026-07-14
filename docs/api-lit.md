# 文献管理增强 API 契约 — 阅读 · 笔记 · AI 伴读 · 引用管理

延续既有总则。所有路由挂 `/api`，JWT Bearer，权限=项目成员（论文级校验复用 `get_paper_for_user`，非成员 404）。

## 1. PDF 阅读

- `GET /papers/{id}/pdf` → `application/pdf` FileResponse（无 PDF 文件 → 404 `PDF_NOT_AVAILABLE`）
- `POST /papers/{id}/fetch-pdf` → `PaperDetail`（按需补下 PDF + 抽全文；仅限有 arxiv_id 的论文，否则 400 `PDF_SOURCE_UNSUPPORTED`；下载失败 502 `PDF_FETCH_FAILED`；已有 PDF 时幂等直接返回）

## 2. 笔记 Notes

- `GET /papers/{id}/notes` → `NoteRead[]`（按 created_at 倒序）
- `POST /papers/{id}/notes` `{content}` → 201 `NoteRead`
- `PATCH /notes/{id}` `{content}` → `NoteRead`（仅作者或平台 admin，否则 403）
- `DELETE /notes/{id}` → 204（同上权限）
- `GET /projects/{pid}/notes?q=&paper_id=&page=&size=20` → `{items: NoteWithPaper[], total, page, size}`

`NoteRead`: `{id, paper_id, project_id, author_id, author_name(display_name 回退 email 前缀), content, created_at, updated_at}`
`NoteWithPaper = NoteRead & {paper_title}`

- 现有 `GET /projects/{pid}/search` keyword 模式扩展：命中 notes.content 的论文也返回（papers 结果并集）
- Obsidian 导出（`build_obsidian_zip`）：有笔记的论文页追加 `## 笔记` 小节，每条 `> **{author_name}** ({YYYY-MM-DD})` + 正文

## 3. AI 伴读 Chat

- `POST /papers/{id}/chat` `{question, history: [{role: "user"|"assistant", content}]}` → **SSE 流**
  - 事件：`delta` `{text}`（增量文本）… `done` `{usage: {prompt_tokens, completion_tokens}}`；错误 `error` `{detail}` 后关流；15s 心跳注释
  - 上下文：优先 full_text（截断 80_000 字符，头尾各留），否则 wiki_content，否则 abstract；system prompt 要求只依据论文内容回答、不确定要明说、中文回答
  - LLM stage 新增 `reading`（STAGES + 前端 LLM_STAGES 同步加；无路由走 default）；用量记账 project_id
  - history 无状态由前端携带（最多带最近 10 轮）

## 4. 手动添加文献

- `POST /projects/{pid}/papers` body 三选一：`{arxiv_id}` | `{doi}` | `{bibtex}`（bibtex 为单条条目文本）
  - arxiv_id → ArxivClient.fetch_by_ids；doi → OpenAlex 反查；bibtex → bibtexparser 解析（title 必需，author/year/journal|booktitle→venue/doi/url 尽量取）
  - 创建 `source="manual"`、`status="included"`；有 arxiv_id 的自动尝试补下 PDF（失败不阻塞创建）
  - 项目内按 arxiv_id 或 doi 去重：已存在 → 409 `{detail: "PAPER_EXISTS", paper_id}`
  - 解析失败 → 422 `{detail: "PARSE_FAILED: <原因>"}`
  - 成功 → 201 `PaperDetail`

## 5. 标签与个人状态

- `GET /projects/{pid}/tags` → `[{id, name, paper_count}]`
- `PUT /papers/{id}/tags` `{names: string[]}` → `PaperDetail`（整组覆盖；新名字自动建 tag；空数组=清空；tag 无论文引用时自动清理可延后）
- 个人状态：`PUT /papers/{id}/my-meta` `{starred?: bool, reading_status?: "unread"|"reading"|"read"}` → `{starred, reading_status}`
- `GET /projects/{pid}/papers` 列表扩展 query：`tag=<name>&starred=true&reading_status=`；`PaperRead` 增加字段：`tags: string[]`、`starred: bool`、`reading_status: string`（后两者为当前用户视角，无记录默认 false/"unread"）、`note_count: int`

## 6. 引用导出

- `GET /projects/{pid}/export/citations?format=bibtex|csl-json&status=&tag=&starred=`
  - `bibtex` → `text/plain` .bib 下载；citation key = `{第一作者姓小写}{year}{标题首个实义词小写}`（冲突加 a/b/c 后缀）；entry 类型：有 venue 且含 proceedings/conference→inproceedings，有 journal→article，否则 misc；arxiv 论文带 `eprint`/`archivePrefix=arXiv`
  - `csl-json` → `application/json` 数组（Zotero 可直接导入）：id=citation key、type、title、author:[{family,given}]、issued:{date-parts}、DOI、URL、container-title
  - 过滤参数与论文列表一致；缺省导出 status in (compiled, included)

## 7. 前端要点

- 新路由 `/papers/:id/read` 阅读工作台：左 PDF（fetch blob→objectURL→iframe，加载态/无 PDF 引导「获取 PDF」按钮）+ 右侧 Segmented 三面板：笔记（列表+编辑器 textarea/预览切换）/ AI 伴读（气泡+流式+存为笔记）/ 论文信息（复用 wiki 详情渲染）
- wiki 页：论文详情加「阅读」主按钮；PapersTab 工具栏加「添加文献」（Modal 三方式 Segmented）与「导出」下拉（Obsidian zip / BibTeX / CSL-JSON）；行与详情展示 tags/星标/阅读状态/笔记数；列表过滤器加 标签/星标/阅读状态
- wiki 页第四个 Tab「笔记 Notes」：项目笔记本（搜索 + 分页 + 点击跳论文阅读页）
