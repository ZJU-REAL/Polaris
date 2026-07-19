# M5-B API 契约 — 论文撰写（前后端共同遵守）

延续既有总则。基于已批准 M5 计划 Wave 2。CRDT 冒烟已通过（pycrdt.websocket 的 ASGIServer 挂 FastAPI 可用）。

## 1. Manuscripts

- `GET /manuscripts/templates` → `[{key: "neurips2026"|"iclr2026"|"acl", name, page_limit, sections: [..]}]`
- `POST /projects/{pid}/manuscripts` `{title, template, idea_id?, experiment_id?}` → 201 `ManuscriptRead`（从模板 pack 展开 files；自动构建 fact-pack）
- `GET /projects/{pid}/manuscripts` → `ManuscriptRead[]`
- `GET /manuscripts/{id}` → `ManuscriptDetail`
- `PATCH /manuscripts/{id}` `{title?}`；`DELETE`（仅 owner/admin）

`ManuscriptRead`: `{id, project_id, idea_id, experiment_id, title, template, status: draft|writing|compiled|under_review|approved|submitted, created_at, updated_at}`
`ManuscriptDetail = ManuscriptRead & {files: [{id, path, size, updated_at}], fact_pack, latest_compile: CompileResult|null, writing_voyage_id|null}`

## 2. 文件

- `GET /manuscripts/{id}/files/{fid}` → `{id, path, content}`（编辑器初始加载用；实时同步走 WS）
- `POST /manuscripts/{id}/files` `{path, content?}` → 201；`DELETE /manuscripts/{id}/files/{fid}`；`PATCH` `{path}` 重命名
- 模板样式文件（.sty/.cls/.bst）标记只读：`files[].readonly: true`，不可改删

### 2b. 文件版本历史

- 自动打点（同文件与上一份内容相同则跳过；每文件上限 50，删最旧）：AI 分节写入前（origin=`pre_ai`）、每次编译当刻（`compile`，label=`编译 vN`）、恢复前备份（`pre_restore`）
- `GET /manuscripts/{id}/files/{fid}/versions` → `[{id, seq, origin, label, size, created_by, created_at}]`（新在前）
- `GET .../versions/{vid}` → 上述元数据 + `content`
- `POST .../versions/{vid}/restore` → 先把当前内容备份为 pre_restore 快照，再整文件替换（有活跃 CRDT 房间经 Y 事务，协同者实时可见）；readonly 文件 409 `FILE_READONLY`，版本不存在 404 `VERSION_NOT_FOUND`

## 3. fact-pack（防幻觉事实源）

```json
{
  "idea": {"title", "summary"},
  "hypotheses": [{"text", "status", "evidence"}],
  "metrics": [{"name", "runs": [{"seq", "value"}], "best"}],
  "figures": [{"fig_id": "exp_fig_0", "caption", "source": "experiment"}],
  "citations": [{"bibkey", "title", "year"}],
  "generated_at": iso
}
```

- `POST /manuscripts/{id}/fact-pack/refresh` → 重新从 experiment+文献库组装（figures 取实验图表；citations 取项目库 status in compiled/included 的全部论文，bibkey 用 citations.py 规则）；重建时保留 `revision_notes`（评审修订说明）
- AI 起草（`POST /draft`）前后端**自动**重建 fact-pack，库/实验更新后无需手动刷新
- 编译时自动生成 `references.bib`（citations.py build_bibtex）与 `figures/` 目录（实验图 PDF 复制），均为只读虚拟文件

## 4. 编译

- `POST /manuscripts/{id}/compile` → **同步**（tectonic 装进 api+worker 镜像，硬超时 120s，≤3 趟）→ `CompileResult`
- `CompileResult`: `{version, status: ok|error|timeout, pdf_available, diagnostics: [{severity: error|warning, file, line|null, rule: undefined_citation|undefined_reference|latex_error|overfull|other, message}], compiled_at, duration_ms}`
- `GET /manuscripts/{id}/pdf` → 最新成功版 PDF（inline FileResponse）；`GET /manuscripts/{id}/compile/latest` → CompileResult
- 编译产物存 `{data_dir}/manuscripts/<id>/v<version>/`

## 5. AI 起草（写作任务）

- `POST /manuscripts/{id}/draft` `{sections?: null|["introduction",...], notes?: str}` → `VoyageRead`（kind=`paper_writing`，同 manuscript 互斥 409）
- 管线（stage=writing）：分节固定顺序 Intro→Method→Experimental Setup→Results→Conclusion→Abstract→（编译）→Related Work（检索文献库+S2 命中集内选引）→ 终编译，全文编译 ok 为完成条件
- 每节输出静态校验（违规重写 ≤2）：`\cite{k}` 必须∈fact_pack.citations；`\includegraphics` 只能引用 fact_pack.figures 的 fig_id；正文数字（\d+\.?\d*% 与小数）必须能在 metrics 表内找到（±0.01 容差）或在白名单（年份/章节号等启发式豁免）
- 重写用尽仍违规**不判整单失败**：降级写入最后一稿并在节顶加 TODO 注释（step 结果含 `needs_review`/`violations`），记 Activity `manuscript.section_needs_review`，继续写后续节
- 每节一轮 self-reflection 精修；权威落库经 `apply_ai_edit`（worker 无活跃房间直接写库 + 版本快照）
- status 流转：draft→writing→compiled；流转推 WS `manuscript.status`（前端靠它实时刷新，仅留 30s 慢轮询兜底）

### 5a. 流式直播（AI 光标实时撰写）

- worker 与 API 是独立容器，CRDT 房间只在 API 进程。撰写时 worker 把 LLM 增量按分节发布到 redis `crdt:stream`（`{op: open|delta|replace, file_id, section, text}`）；API 进程的 `CRDTStreamSubscriber`（随 lifespan 启停）订阅后写进**活跃房间**的分节区间 → 连接中的编辑器逐字看到 AI「打字」。无活跃房间时 no-op（worker 的 DB 写为准）
- 房间写入维持「正文末尾恒有一个换行」不变式：`open` 清空占位、`delta` 插到末换行前、`replace` 整节规范化覆盖（收尾/重写/精修对齐，房间与 DB 收敛到同一节正文）
- 撰写相位经 `ctx.notify` 推 WS `manuscript.ai_writing {manuscript_id, file_id, section, phase}`，`phase ∈ typing|revising|done|compiling`。前端据此在编辑器画「✨ AI」光标（脉动竖条 + 标签 + 当前小节整行高亮 + 自动滚动跟随），并在顶栏显示状态条；跨文件时给「到 X 看 AI 撰写」跳转。`done` 或 `status!=writing` 时收起
- 直播为**尽力而为**：redis 不可达或无人观看时静默降级，不影响起草与权威落库

### 5b. 内联 AI 写作辅助（SSE）

- `POST /manuscripts/{id}/assist` `{mode: polish|rewrite|continue, text?, instruction?, before?, after?}` → SSE 流：`delta {text}`* → `warnings {items}`?（越界 \cite/\includegraphics 提示，不阻断）→ `done {usage}`；异常转 `error {detail}` 后关流；15s 心跳注释
- 校验：polish/rewrite 需 `text`（422 `ASSIST_TEXT_REQUIRED`）；rewrite 需 `instruction`；continue 需 `before`
- stage=`writing`；prompt 注入事实包速览（citations bibkey / metrics / figures），结果由人审后应用（替换选区或插入光标处），不强制重写

## 6. 协同编辑（CRDT）

- `WS /ws/manuscripts/{fid}?token=` — pycrdt.websocket YRoom（房间名=file id）；on_connect 校验 JWT + 项目成员 + 文件存在且非 readonly
- Y doc 结构：`Text` 命名 `"content"`；服务器加载时若房间新建则从 ManuscriptFile.content 初始化
- 快照：文档更新防抖 2s 写回 ManuscriptFile.content（编译/AI/REST 读到的即最新）；服务重启后房间从库重建
- 前端：yjs + y-codemirror.next 绑定 CodeMirror6；awareness 显示协作者光标（用户名+颜色）

## 7. 投稿

- `POST /manuscripts/{id}/submit` → 创建 `paper_submission` Gate（payload 含 manuscript_id/title/最新编译版本）；前置校验：latest_compile.status=ok，否则 409 `COMPILE_REQUIRED`；审批通过 → status=submitted（Wave 3 将加评审通过前置）

## 8. 前端 Writer 页（替换占位）

- 论文列表（项目内）+「新建论文草稿」Modal（标题/模板选择/关联 idea+experiment 下拉）
- 编辑工作台 `/writer/:id`：左文件树（增删改名，readonly 文件锁图标）/ 中 CodeMirror6（LaTeX 高亮 legacy stex、y-collab、多人光标、Cmd+S 触发编译）/ 右侧上下分栏：PDF 预览（blob iframe，编译后自动刷新）+ 诊断面板（severity 图标、点击跳转对应文件行）
- 顶栏：「AI 起草」（Modal 可选节+备注 → draft 接口，进行中显示任务链接与禁用）、「编译」（loading 转圈+耗时）、「事实包」抽屉（fact_pack 分区展示+刷新按钮）、「投稿」（审批确认）
- 协作者在线指示（awareness 头像点）

## 9. 测试要点

- 后端：模板展开/fact-pack 组装/编译诊断解析（真实 tectonic 日志 fixture）/静态校验三类违规拒绝/写作 voyage 分节顺序与 Related Work 延后（fake LLM 桩）/CRDT 快照落库（pycrdt 直连房间写入→防抖后库内容一致）/submit 前置
- tectonic 不可用环境（本地 venv 测试）：编译走 subprocess mock；真实编译在 docker 验证
- 前端：tsc/build；协同编辑真机双窗口验证

---

## 10. 模板库 / 文件管理器 / arXiv 导出 / 协作者（Overleaf-like 增强，本分支新增）

### 10.1 模板库（DB 化）
统一 `TemplateInfo{id, name, description, source(builtin|seeded|uploaded), scope, project_id, engine, page_limit, sections[], unofficial, downloadable, downloaded, download_key, file_count}`；`id` 内置=key、库内=uuid，创建稿件时作为 `template` 传回。
- `GET /manuscripts/templates?project_id=` — 内置 + 全平台 + 该方向私有上传模板 + **manifest 里尚未下载的官方模板**（伪条目 `id="seed:<key>"`、`downloaded:false`、`download_key:<key>`、`file_count:0`；不能直接拿它建稿，须先下载）
- `POST /manuscripts/templates`（multipart：file=zip, name, description?, engine?, page_limit?, project_id?）→ 201 TemplateInfo。给 project_id=项目私有（需成员）；否则全平台（需平台 admin，否则 403 ADMIN_REQUIRED_FOR_GLOBAL）。zip 无 .tex → 422
- `GET /manuscripts/templates/{id}/download` → zip；`DELETE /manuscripts/templates/{id}`（创建者/项目管理者/admin）
- **按需自动下载官方模板（首次使用触发，带进度条）**：
  - `POST /manuscripts/templates/download/{key}` → `TemplateDownloadProgress{key,name,phase(pending|downloading|extracting|done|failed),percent,detail,template_id?,error?}`，幂等（已下载直接回 done 带 template_id）；后台任务下载；未知 key → 404
  - `GET /manuscripts/templates/download/{key}/progress` → SSE：`progress`* → `done{template_id}` / `error{detail}`。zip 走 httpx 流式（content-length 算百分比），git 解析 `Receiving objects: NN%`
- `POST /manuscripts/templates/seed`（仅 admin）→ 一次性拉取全部官方模板，幂等，返回逐条 seeded|skipped|failed
- 实现：库内模板文件落 `data_dir/templates/<id>/files/`（二进制安全）；官方模板通常无 `% POLARIS_SECTION` 标记 → AI 分节起草降级（用内联 AI/手写）；下载进度存 API 进程内存（多进程部署需改 redis/worker）

### 10.2 文件管理器（文件夹 + 上传 + 二进制）
`ManuscriptFileBrief` 增 `is_binary`/`is_folder`。二进制字节落 `data_dir/manuscripts/<id>/assets/<path>`，`content` 为空、只读。
- `POST /manuscripts/{id}/folders` `{path}` → 文件夹占位（删除时级联删该目录下文件）
- `POST /manuscripts/{id}/files/upload`（multipart：file, path?）→ 文本入 content（可编辑），二进制落盘（只读）
- `GET /manuscripts/{id}/files/{fid}/raw` → 二进制原始字节（图片/PDF 预览、下载）
- 编译组装（assemble_workdir）会把二进制资源与文件夹一并写入编译目录；`figures/` 仍为实验图保留前缀

### 10.3 arXiv 清洁包导出
- `GET /manuscripts/{id}/export/arxiv` → tar.gz（源文件 + references.bib + figures + **.bbl**，剔除 aux/log/pdf/out/blg/synctex）。导出时**重编一遍**以生成与当前源一致的 .bbl；提示放响应头 `X-Export-Notes`

### 10.4 协作者 / 共享编辑链接
稿件权限 = 所属研究方向成员；协作操作落到项目。
- `GET /collaborators/search?q=` → `[{id,email,display_name}]`（注意不用 /users/search，会被 fastapi-users /users/{id} 抢占）
- `GET /manuscripts/{id}/collaborators` → `[{user_id,email,display_name,role,is_owner}]`
- `POST /manuscripts/{id}/collaborators` `{user_id, role?}` → 更新后列表（需 owner/admin；用户不存在 404）
- `DELETE /manuscripts/{id}/collaborators/{user_id}`（需 owner/admin；不能删 owner → 409）
- `POST /manuscripts/{id}/share-link` `{expires_days?=14, max_uses?}` → `{token, join_path:/join/{token}, expires_at, max_uses}`；复用研究方向邀请，平台用户打开 `/join/{token}` 登录加入即获协同编辑权（只读匿名链接暂不支持）
