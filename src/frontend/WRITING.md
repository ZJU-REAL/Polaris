# Interface writing and layout guide

Polaris is a desktop app for one researcher. Every word on screen should help that person decide or act. If a sentence explains how Polaris works inside, it belongs in the docs, not the interface.

All copy goes through `tr(zh, en)`. Chinese and English are written separately, each natural in its own language; the English is not a translation of the Chinese word for word.

## Principles

1. **Say what it does for the user, not how it works.** Pipelines, thresholds, model names, scoring steps and storage details stay out unless the user is choosing them.
2. **Shortest wording that is still unambiguous.** Cut words that don't change the meaning: "you can", "here", "automatically", "currently", "please note".
3. **One idea per sentence, one sentence per hint.** A section description is at most one sentence. If more is truly needed, link to the docs ("了解更多 / Learn more").
4. **Same thing, same word, everywhere.** Use the glossary below.
5. **Calm and neutral.** No exclamation marks, no emoji or ✨, no reassurance ("别担心", "这是正常的"), no rhetorical asides ("——不是…也不是…"), no stacked parentheses.
6. **AI is a fact, not a selling point.** Mention AI only where the user needs to know content is generated (for example, labelling a generated summary).

## Patterns

| Element | Chinese | English | Notes |
|---|---|---|---|
| Page and section titles | Noun, ≤ 8 characters: `文献库`, `模型与智能体` | Title Case for page titles, sentence case for sections | No trailing punctuation |
| Buttons | Verb or verb + object: `保存`, `添加文献库`, `重新生成` | `Save`, `Add library`, `Regenerate` | Never `确定` / `OK` for a destructive or specific action; name the action |
| Field labels | Noun: `名称`, `API 地址` | `Name`, `API URL` | No colon |
| Field hints | One short phrase, only when the label is not enough: `留空则使用默认地址` | `Leave empty to use the default` | Ranges and defaults go in the placeholder or a compact hint, not a paragraph |
| Placeholders | An example, not an instruction: `例如 arXiv:2005.11401` | `e.g. arXiv:2005.11401` | |
| Section descriptions | One sentence, ≤ 40 characters | One sentence, ≤ 20 words | Describe the outcome |
| Empty states | One line saying what will appear here + one action | Same | `还没有文献库` + `[新建文献库]` |
| Loading | `加载中…` / `正在导入…` | `Loading…` / `Importing…` | |
| Success toasts | `已保存`, `已删除 3 篇` | `Saved`, `Deleted 3 papers` | Past tense, no period |
| Errors | What failed + what to do: `无法连接模型服务，请检查 API 地址` | `Couldn't reach the model service. Check the API URL.` | No internal terms (backend, worker, 500, stack). Raw detail may appear in a secondary monospace line |
| Confirm dialogs | Title: question naming the object `删除文献库「X」？`; body: the consequence in one sentence `其中的 120 篇论文会移入回收站。`; buttons `删除` / `取消` | `Delete library "X"?` / `Its 120 papers move to the trash.` / `Delete` / `Cancel` | Destructive button uses the danger style |
| Status labels | Short state: `运行中`, `已暂停`, `失败` | `Running`, `Paused`, `Failed` | |
| Counts | `12 篇论文`, `3 个概念` | `12 papers`, `3 concepts` | Handle singular in English |

## Typography

- Chinese: full-width punctuation `，。：；？（）「」`. A space between Chinese and Latin letters or digits: `12 篇`, `arXiv 编号`.
- No `。` at the end of labels, buttons, titles and toasts. Full-sentence hints and descriptions end with `。`.
- English: sentence case for everything except page titles; curly quotes and apostrophes in visible text (`’`); `…` not `...`.
- Keyboard shortcuts as `⌘K` / `Ctrl K`.

## Glossary

| Use | 中文 | English | Don't use |
|---|---|---|---|
| A research topic the user works on | 课题 | Topic | 项目, project, workspace |
| A collection of papers on a direction | 文献库 | Library | 方向库, 库（alone in headings） |
| The user's own saved papers | 我的文献库 | My library | 个人库, 书架 |
| A long-running background job | 任务 | Task | 航程, voyage, job, run (in UI) |
| A generated paper summary page | 解读 | Summary | 编译, compiled wiki, wiki page |
| Generate that summary | 生成解读 / 重新生成 | Generate summary / Regenerate | 编译, compile |
| A concept extracted across papers | 概念 | Concept | |
| An external coding agent | 智能体 | Agent | ACP backend, 后端 |
| A configured model service | 模型服务 | Model provider | API（alone）, provider（in Chinese） |
| A step that needs the user's go-ahead | 审批 | Approval | gate, 闸门 |
| Polaris's built-in assistant | 助手 | Assistant | Buddy（except the product name in one place）, AI 助理 |
| The local background process | 本机引擎 | Local engine | backend, server, worker, 服务端, 后端 |
| AI usage | 用量 | Usage | token（except inside a usage number: `1.2k tokens`） |

Words that must not appear in the interface at all: 账号, 注册, 登录, 管理员, 共享给他人, 服务器部署, worker, uuid, API paths, environment variable names, file paths inside the repository, `docs/*.md`. (Developer-facing settings such as the MCP connection snippet may show code, inside a code block.)

## Layout

- **One primary action per view.** Other actions are secondary or tertiary buttons, or go into a "more" menu.
- **Hierarchy through type and space, not boxes.** Avoid cards inside cards and borders around every group. Use the spacing and type tokens in `styles/tokens.css`; no new magic numbers in inline styles where a token exists.
- **Help text is quiet.** Hints use the secondary text colour and small size, sit under the field, and never exceed two lines at 820 px.
- **Consistent states.** Every list and panel has the same loading, empty and error treatment: `LoadingState`, `EmptyState` and `ErrorState` from `components/ui/EmptyState.tsx` (or the `.empty` / `.empty.is-error` classes for inline cases).
- **Error text comes from one place.** Show API errors with `errorText(e)` from `lib/errors.ts`, which turns error codes into a plain sentence. Don't print `e.message` directly; it is often a raw code.
### Visual style

Quiet chrome, colour only on content (reference: Agentero). All values are tokens in `styles/tokens.css`.

- **Type:** `--fs-caption` 11 / `--fs-sm` 12 / `--fs-body` 13 / `--fs-title` 15 / `--fs-h2` 17 px. No half sizes (12.5, 11.5). Weights 400 body, 500 labels/buttons/nav, 600 titles; never 650/700.
- **Colour:** `--text` for content, `--text-2` for secondary, `--text-3` for hints and meta. Never `--text-4` for readable text. Brand blue only on the one primary button, focus and selection.
- **Controls:** `.btn` 32px, `.btn.sm` 28, `.btn.xs` 24, weight 500. Order: `btn-primary` (one per view) > `btn-ghost` > `btn-soft` > `btn-plain` (borderless) > `btn-danger-soft` (row-level delete). Solid `btn-danger` only in confirm dialogs. Inputs 32px, white.
- **Surfaces:** radii `--radius-sm` 7 (controls), `--radius` 10 (cards), `--radius-lg` 12 (dialogs). Cards are flat (hairline border, no real shadow); shadows only on menus, dialogs, toasts. No cards inside cards.
- **Status:** `StatusDot` / `StatusPill` render an 8px dot + a word. Coloured `.pill` badges (`StatusPill badge`) are for at most one or two per row.
- **States:** `EmptyState` (48px icon tile + one line + at most one button), `LoadingState` (shimmer rows), `ErrorState`. Long help goes in `HelpTip` (“?” next to a label, also `FormField help=`); a field shows at most one visible hint line (`.hint` / `FormField hint=`).
- **Motion:** `--dur-micro` 100 / `--dur-fast` 150 / `--dur-normal` 200 ms with `--ease-out`; dialogs and toasts enter in 150 ms; reduced-motion turns it all off. Spacing uses `--sp-1 … --sp-8` (4px grid).
- **Text never breaks layout.** Long titles truncate with a tooltip; numbers in tables are right-aligned and tabular; nothing overflows horizontally at 820 px.
- **Badges are rare.** A row shows at most two badges; prefer plain text for secondary metadata (`3 天前 · 12 篇`).
- **Remove what no longer applies.** Single-user, local-only: no account, sign-up, sharing or server UI.
