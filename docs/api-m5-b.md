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

- `POST /manuscripts/{id}/fact-pack/refresh` → 重新从 experiment+文献库组装（figures 取实验图表；citations 取项目库 status in compiled/included 的全部论文，bibkey 用 citations.py 规则）
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
- 每节一轮 self-reflection 精修；写入对应 ManuscriptFile **经 CRDT 事务**（协同者实时可见），无房间时直接写库
- status 流转：draft→writing→compiled

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
