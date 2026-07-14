# M5-C API 契约 — 论文评审（前后端共同遵守）

延续既有总则。基于已批准 M5 计划 Wave 3。

## 1. 发起评审

- `POST /manuscripts/{id}/review` `{personas?: null|[{name, stance}]}` → `VoyageRead`（kind=`paper_review`，同 manuscript 互斥 409；前置：latest_compile.status=ok，否则 409 `COMPILE_REQUIRED`）
- 管线（固定计划）：`引用核验 → 事实查错 → 渲染稿件 → 评审员评审(×3) → 汇总(meta-review) → guardrail 校验`
- 结果落一个 `ReviewSession(target_type="manuscript", target_id=manuscript_id)`，`payload` 存核验表/查错表/meta；逐 reviewer 意见与 meta 各为一条 `ReviewMessage`（author_type=agent, author_name=人设名 / "主席 Meta"）

## 2. 核验与查错（结构化，存 session.payload）

```json
{
  "citation_check": {
    "total": 24,
    "items": [{"bibkey", "existence": "exact|minor|fabricated", "matched_title", "source": "library|s2|openalex|none",
               "support": "supported|partial|unsupported|not_checked", "context_snippet"}]
  },
  "fact_check": {
    "items": [{"location": "results.tex:42 或 章节名", "issue", "evidence", "kind": "number_mismatch|unsupported_claim|missing_figure|other", "severity": "major|minor"}]
  },
  "meta": {"soundness", "presentation", "contribution", "rating", "decision_hint": "accept|borderline|reject",
            "summary", "aggregation": {"ratings": [..], "method": "median-outlier-suppressed"}},
  "guardrail": {"passed": true, "regenerated": 0}
}
```

- 引用核验：LaTeX 源解析全部 `\cite` → bib 条目：存在性=库内精确匹配 → 否则 S2/OpenAlex title+author+year 模糊匹配（exact/minor/fabricated）；支撑性=引用语境句（cite 前后 2 句）+ 被引论文摘要（库内有全文取相关段）→ LLM 判定；`fabricated` 存在时评审结论强制不通过
- 事实查错（stage=review）：数字↔fact-pack metrics 确定性比对；claim 抽查（LLM 针对性提问式）；`\ref`/图存在性确定性检查

## 3. 评审员

- 默认三人设：苛刻方法论者 / 建设性领域专家 / 严格实验复现者（可传自定义）
- 输入：编译 PDF 前 9 页渲染 PNG（pymupdf，多模态）+ LaTeX 源正文 + 核验/查错摘要
- 每员输出（JSON 严格校验）：`{soundness: 1-4, presentation: 1-4, contribution: 1-4, rating: 1-10, confidence: 1-5, strengths: [..], weaknesses: [..], questions: [..]}`
- 聚合：rating 取中位数；与中位差 >3 的评分降权 0.5；低 confidence(≤2) 降权 0.5；meta LLM 写 summary（stage=review）
- **guardrail**：每份 reviewer 意见发布前 LLM 校验（是否引用论文实际内容、具体、无幻觉）→ 未过重生成 ≤2 次，仍未过则该 reviewer 意见标记 `unreliable: true` 且不计入聚合

## 4. 结果与流转

- `GET /manuscripts/{id}/reviews` → `[{session_id, created_at, payload.meta 摘要, message_count}]`（历史多轮）
- `GET /sessions/{sid}/messages` 复用 M3；人类讨论：该 session 直接 POST messages（复用），WS review.message 复用
- 评审通过（meta.rating ≥ 6 且无 fabricated）→ Manuscript.review_passed=true（新列）→ submit 前置从 compile-ok 升级为 review_passed（未通过 409 `REVIEW_REQUIRED`）；管理员可跳过（gate 审批时 override）
- 未通过 → weaknesses+查错表自动生成「修订说明」写入 fact_pack.revision_notes → 下次 AI 起草/修订可引用；status 回 compiled

## 5. 前端 Paper Review 页（替换占位）

- 稿件选择（项目内 under_review/compiled 的稿件下拉）+「发起同行评审」按钮（personas 可编辑，同 M3 锦标赛 Modal 风格）
- 评审总览卡：三维度条形（soundness/presentation/contribution 1-4）+ rating 大数字 + decision_hint pill + meta summary
- 逐评审员卡：人设名徽章、四分数 mini 条、strengths（绿区）/weaknesses（红区）/questions（黄区）列表、unreliable 标记灰显
- 引用核验表：bibkey / 存在性三色 pill（exact 绿 minor 黄 fabricated 红）/ 匹配来源 / 支撑性 pill / 语境悬停
- 查错清单：severity 图标 + location（可点击跳 writer 编辑器对应文件）+ issue/evidence 折叠
- 人类讨论区（复用 DiscussionPanel 模式，target=该 session）
- 底部操作：「修订」（跳 writer 并提示 revision_notes 已入事实包）/「申请投稿」（须 review_passed）

## 6. 测试要点

- 核验三态路径（respx mock S2/OpenAlex）、fabricated 强制不通过、支撑性判定 fake 桩
- 查错数字比对确定性单测；reviewer JSON 校验与聚合（降权/中位数）单测；guardrail 拒绝→重生成→unreliable 路径
- 评审通过/不通过流转（review_passed、submit 前置、revision_notes 写入）
- fake provider：reviewer/meta/guardrail/support 判定 marker；现有 160 例不回归
