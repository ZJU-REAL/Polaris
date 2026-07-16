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

## 6.5 论文图片（重要图提取与展示）

- Paper 新列 `figures` JSON：`[{index, page, width, height, caption: str|null, kind: str|null, important: bool}]`（文件路径不出 API，落盘 `<data_dir>/papers/<paper_id>/figures/fig_<index>.png`）
- `kind`：视觉模型判定的图片类型 `motivation|method|architecture|experiment|other`（旧数据/降级提取为 null）；VLM 筛选优先覆盖动机/方法/架构/实验四类，重要图 2-6 张，caption 为 1-2 句中文说明
- `GET /papers/{id}/figures` → 上述数组（无则 `[]`）
- `GET /papers/{id}/figures/{index}/image` → `image/png` FileResponse（成员校验）
- `POST /papers/{id}/extract-figures?force=false` → `{figures: [...]}`：PyMuPDF 提取嵌入图 + **矢量图渲染兜底**（cluster_drawings 找矢量绘图簇按区域渲染，学术论文的架构/曲线图多为矢量）→ 尺寸过滤 ≥200×150、去重、**按页轮转选优**（每页先取最大一张，单页 ≤3、总上限 12）→ 若视觉模型可用（stage=librarian 多模态）挑选重要图（2-6 张）配类型与说明；VLM 失败降级：按面积取前 4 张 `important=true` 无 caption。无 PDF → 404 `PDF_NOT_AVAILABLE`；已有 figures 且非 force → 幂等直返
- `POST /papers/{id}/recompile` 时若一张重要图都没有（多为旧提取逻辑漏掉矢量图）→ 自动重提候选再筛选
- 管线集成：`wiki.fetch_extract` 抽全文时顺带提取候选图；`wiki.compile` 编译成功后对该论文调用同一套筛选注释逻辑（失败不影响编译，figures 留空可后补）
- LLM 层：`complete()` 增加可选 `images: list[bytes]`（openai_compat 转 data-url image_url parts；fake 返回确定性筛选 JSON；anthropic 原生格式或 NotImplementedError 均可）
- `PaperDetail` 增加 `figures` 字段

## 6.6 图文交织 wiki（blog 式）

- wiki_content 支持行内图片标记 `![[fig:N]]`（N=figures 数组里的 index）：编译时 Librarian **看图写作**——先筛选注释重要图（含类型），再把选中图（≤6 张，多模态）与图注/类型一起给编译调用；后端校验标记引用的 index 存在（无效标记剥除）
- **图文交织约束**：清单里每张图都必须插入，按类型落位到对应小节（动机图→研究背景与动机、方法/架构图→方法、实验图→实验与结果），且插图前后必须有 1-3 句文字介绍该图（画了什么、支撑什么论点、看哪个部分）；有配图但首稿一张没插 → 带强指令自动重写一次，仍失败接受纯文字稿（图库兜底展示）
- 前端图注（嵌入图/缩略图/lightbox）显示类型标签（动机图/方法图/架构图/实验图）
- `POST /papers/{id}/recompile` → `PaperDetail`（重跑筛选注释+图文编译，覆盖 wiki_content；无全文时用摘要降级；同步调用，约 1 分钟）
- 图片提取修复：合并 PDF SMask 软蒙版，透明区域铺白底（Pillow），杜绝黑底图；force 重提会重新生成图片文件
- Obsidian 导出：figure PNG 打包进 `papers/figures/`，`![[fig:N]]` 重写为相对路径的标准 markdown 图片
- 前端 Markdown 渲染器支持该标记：渲染为居中图 + 灰色图注（点击开 lightbox）；代码块内不解析

## 8. 文献知识底座 — 全文分段索引 · 文献库对话 · 图谱

支撑文献问答 / idea 生成等知识服务的检索底座；全部为确定性代码 + stage=reading 的判断性回答。

### 8.1 全文分段索引（paper_chunks）

- 新表 `paper_chunks`：`{id, paper_id, project_id, seq, text, embedding vector(1024)|null}`，`(paper_id, seq)` 唯一
- 切分：确定性代码（`services/chunks.py`）——段落边界贪心打包 ~1200 字符、超长段 1600 硬切带 150 重叠、单篇上限 120 段
- 管线集成：`wiki.fetch_extract` 抽全文后自动分段；`wiki.link_concepts` 批量补齐 chunk embedding（observation 增加 `chunks_embedded`/`chunk_embed_error`）；`POST /papers/{id}/fetch-pdf` 补下 PDF 时同样分段
- `POST /projects/{pid}/index/rebuild` → `{papers_indexed, chunks_created, embedded, embed_error, total_chunks}`：给已有全文但缺分段的论文补索引（幂等，已有分段的跳过）

### 8.2 文献库对话

- `POST /projects/{pid}/chat` `{question, history}` → **SSE 流**（与论文伴读同款事件语义）
  - 事件顺序：`sources` `{items: [{index, paper_id, title, year, status, relevance, concepts: [概念名]}]}` → `delta` `{text}`… → `done` `{usage}`；错误 `error` `{detail}` 后关流；15s 心跳注释
  - 检索：问题向量化 → pgvector 余弦取 top-16 分段（postgres）；不支持向量时降级分词 ilike 关键词打分；一段都没有时退化用高分论文 TL;DR/摘要（旧数据兜底）
  - **检索绝不 500**：向量/关键词检索任一失败（表未迁移、embedding 挂了等）都 rollback 后逐级降级
  - 上下文按论文分组编号（≤8 篇）并附概念清单，system prompt 要求只依据资料回答、句末用 [n] 标注引用、概念用 [[双链]] 标注（仅限清单内名字）、主动做跨文献对比归纳
  - LLM stage 复用 `reading`；用量记账 project_id
- `POST /projects/{pid}/index/rebuild` 缺表时 → 503 `DB_MIGRATION_REQUIRED`
- 前端：文献追踪「文献对话 Chat」Tab——气泡 + 流式；回答内嵌可交互元素：[n] 引用角标（点击开论文）、[[概念]] 双链（点击跳概念库）；来源卡带年份/相关度/概念 chips 与「详情/阅读」操作；「补建全文索引」按钮

### 8.3 知识图谱

- `GET /projects/{pid}/graph` → `{nodes, edges, paper_total, truncated}`
  - 节点：论文（scored 及之后状态，按相关度 top 150）/ 概念（paper_concepts 关联）/ 作者（authors JSON，按关联论文数 top 80，单篇取前 8 位署名）
  - 边：`paper_concept`（上链）、`paper_author`（署名）
- 前端：「图谱 Graph」Tab，三种视图 + 全局「子主题」过滤（选一个概念只看其牵出的论文子图）：
  - 网络：零依赖 canvas 力导向图（分层径向初始 + 碰撞防重叠；平移/缩放/拖动节点/点选高亮邻居/类型过滤/搜索高亮；概念节点可「只看这个子主题」）
  - 时间线：论文按发表年份分列的整齐排布（列内按相关度排序）
  - 主题：按概念聚类的分组网格（top 12 概念 + 未归类区）

### 8.5 论文库状态口径

- 状态机不变（candidate → scored|excluded → fetched → compiled；included/excluded 可人工覆盖），但用户视角重新分组：
  - **库内 `library`** = scored+fetched+compiled+included（相关性达标及之后）——论文库默认视图与计数口径
  - **待解读 `pending_compile`** = scored+fetched；**已解读** = compiled；**人工精选** = included；**未筛选** = candidate；**已排除** = excluded
- `GET /projects/{pid}/papers` 与引用导出的 `status` 参数支持组别名 `library` / `pending_compile` / `compiled_any`（compiled+included，含历史人工纳入）
- `paper_counts` 新增 `library` / `pending_compile` 字段；前端论文库 Tab 计数用 `library`（不再把候选/排除算进"论文库数量"）
- **前端论文库只展示达标文献**：视图仅 全部（library）/ 已编译（compiled_any）/ 已星标（library+starred）；低相关与未筛选论文不出现在论文库；详情操作为 编译|重新编译 / 删除（纳入/排除按钮移除，PATCH 接口保留）

### 8.6 删除文献 · 垃圾桶 · 多选

- **删除语义**：删除先进垃圾桶（= status excluded，可召回）——手动删除与相关性不足自动删除同桶；彻底删除才清数据
- `PATCH /papers/{id}` `{status: "excluded"}`：移入垃圾桶（详情「删除」按钮）
- `POST /papers/{id}/restore` → `PaperDetail`：召回（有介绍回 compiled、打过分回 scored、否则按人工精选 included）
- `DELETE /papers/{id}` → 204：**彻底删除**（清理 PDF/全文/图片文件，分段/笔记/标签/概念关联级联删除）
- `POST /projects/{pid}/papers/batch-delete` `{paper_ids, hard?}` → `{deleted}`：默认软删进垃圾桶；`hard=true` 彻底删除（≤500，非本项目 id 忽略）
- `POST /projects/{pid}/trash/empty` → `{deleted}`：清空垃圾桶（彻底删除全部 excluded）
- 引用导出支持 `ids=<逗号分隔>` 精确导出（多选导出）
- 前端：列表底部固定操作栏（添加文献 / 导出 / 多选 / 垃圾桶）；复选框默认隐藏，「多选」开启后出现；垃圾桶弹窗支持逐篇召回、逐篇彻底删除、清空（带确认）

### 8.7 高级检索与发表机构

- Paper 新列 `affiliations` JSON（发表机构 `["MIT", ...]`）：ingest 的 fetch_extract 步骤对 top-N 论文经 OpenAlex（arxiv/DOI 反查 authorships[].institutions）尽力补充；手动 DOI 导入直接带上；arXiv 原始元数据无机构
- `GET /projects/{pid}/papers` 高级检索参数：`author=`（作者名包含匹配）`affiliation=`（机构包含匹配）`published_from/to=`（发表时间，无 published_at 的按 year 兜底）`created_from/to=`（入库时间），均可与既有过滤组合
- `PaperRead` 增加 `affiliations`
- 前端：搜索框右侧「高级检索」图标 → 展开条件面板（作者/机构/发表时间/入库时间），有条件时图标带蓝点

### 8.4 研究方向管理（补充）

- `DELETE /projects/{id}` → 204（owner / 平台 admin；方向下论文、概念、任务等 FK 级联删除；成员 403、非成员 404）
- `PaperRead` 增加 `compiled_at`（wiki 编译时间；未编译 null）

## 7. 前端要点

- 新路由 `/papers/:id/read` 阅读工作台：左 PDF（fetch blob→objectURL→iframe，加载态/无 PDF 引导「获取 PDF」按钮）+ 右侧 Segmented 三面板：笔记（列表+编辑器 textarea/预览切换）/ AI 伴读（气泡+流式+存为笔记）/ 论文信息（复用 wiki 详情渲染）
- wiki 页：论文详情加「阅读」主按钮；PapersTab 工具栏加「添加文献」（Modal 三方式 Segmented）与「导出」下拉（Obsidian zip / BibTeX / CSL-JSON）；行与详情展示 tags/星标/阅读状态/笔记数；列表过滤器加 标签/星标/阅读状态
- wiki 页第四个 Tab「笔记 Notes」：项目笔记本（搜索 + 分页 + 点击跳论文阅读页）
