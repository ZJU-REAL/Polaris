# Git 工作流

这套规范用来避免本地历史失控、分支交叉污染、迁移 id 撞车等问题。速览版在 `CLAUDE.md`。

## 核心原则

> **`main` 是 origin/main 的只读镜像，本地永远不在它上面 merge 功能分支或 commit。**

以前"为了本地预览把功能分支 merge 进 main"是一切混乱的根源：本地 main 领先 origin 几十个提交、塞满 merge 壳、功能互相污染。断掉这条，绝大多数问题不再发生。

三条铁律：

1. **不往 `main` 上 merge**（main 只做 fast-forward）。
2. **不 `git merge main` 进功能分支**（要追上 main 用 `rebase`）。
3. **迁移 id 用随机哈希**（不手排"滚动规律"id）。

## 分支模型

- 一个功能 = 一分支 = 一 worktree = 一 PR。
- 分支**永远从最新的 `origin/main`** 开，**短命**（尽快合、尽快删）。
- 命名：`feat/xxx`、`fix/xxx`、`chore/xxx`、`docs/xxx`。
- 不在功能分支之间互相 merge（这会让 A 功能的代码漏进 B 功能的 PR）。

```bash
git fetch origin
git worktree add ../wt/feat-x -b feat/x origin/main
# 开发 → commit → push
gh pr create --draft
# 合并后
git worktree remove ../wt/feat-x && git branch -d feat/x
```

## 提交 / PR / issue 语言规范

- **commit message 用英文** conventional commits：`feat/fix/chore/docs/refactor(scope): ...`。
- **PR 与 issue 一律用英文**（标题 + 正文）：面向仓库协作者，保持一致、可检索。
  - issue 先行、PR 用 `Closes #N` 关联；一个功能一条 issue、一个 PR。
- **不写 AI 编码工具信息**：commit / PR / issue 中都不加 `Co-Authored-By: Claude`、
  "Generated with Claude Code" 之类的署名或尾注。

## 同步 main

`main` 只做 fast-forward，落后就拉、领先就是错：

```bash
git checkout main && git pull --ff-only
```

## 追上 main（功能分支）

分支落后 main 时，用 **rebase**（把自己的提交挪到最新 main 之上），**不要 merge main**：

```bash
git fetch origin
git rebase origin/main
# 分支已 push 过：
git push --force-with-lease
```

`git merge main` 会把 main 上**别的功能**一起拖进你分支——历史上 reading 就是这么漏进 writer 分支、导致 PR diff 混入无关文件的。

## 本地预览（不碰 main）⭐

docker dev compose 的源码挂载走 `DEV_SRC`（默认仓库根 `..`，即主 checkout）。要预览某个分支而不污染 main：

```bash
# 一次性：建一个专用 dev worktree
git worktree add ../wt/dev origin/main

# 每次预览：在 dev worktree 里切到目标分支
git -C ../wt/dev fetch origin
git -C ../wt/dev checkout feat/x     # 或 pull 最新

# 指向 dev worktree 起栈（主 checkout 始终停在干净的 main）
DEV_SRC=../wt/dev docker compose -f deploy/docker-compose.yml -f deploy/docker-compose.dev.yml up -d
```

> 容器内 `node_modules` 是匿名卷，和宿主机分离，所以 dev worktree 不需要本地 `npm install`。新增前端依赖后仍需 `up -d --build --force-recreate --renew-anon-volumes frontend`（见 compose 内注释）。

不设 `DEV_SRC` 时行为不变（挂主 checkout），向后兼容。

## Alembic 迁移

- **用随机 id**：`alembic revision -m "描述"`（生成随机 12 位 hex）。**不要手排** `d2e3f4a5b6c7 → e3f4a5b6c7d8` 这种可预测的"滚动 id"——两个平行分支各建"下一条迁移"会算出同一个 id 而撞车。
- 合并前确认迁移的 `down_revision` 接在 **`origin/main` 的最新 head** 上；main 期间又并入了别的迁移就把 `down_revision` 改挂过去。
- 合并前跑一遍 `alembic upgrade head` + downgrade 往返（`backend/tests/test_migrations.py`）确认单 head、无重复建表。
- 生产/本地 DB 若因迁移重排出现"版本指针和实际 schema 不一致"，用 `alembic stamp <当前真实所在>` 校准版本指针，再 `upgrade head` 补新迁移，避免重复建表。

## 部署

- 生产（zju-54）**只从 `origin/main`** 部署：`git pull`（ff）后重启。绝不从本地 main 部署。

## worktree 卫生

- 名字清晰（`../wt/feat-x`），用完 `git worktree remove`。
- 别留 locked / 悬空 worktree；`git worktree prune` 清理失效记录。

## 一页速查

| 场景 | 命令 |
| --- | --- |
| 同步 main | `git checkout main && git pull --ff-only` |
| 开新功能 | `git worktree add ../wt/feat-x -b feat/x origin/main` |
| 追上 main | `git rebase origin/main`（**不是** merge） |
| 本地预览 | dev worktree 里 `git checkout feat/x` + `DEV_SRC=../wt/dev docker compose … up` |
| 建迁移 | `alembic revision -m "..."`（随机 id） |
| commit / PR / issue | 一律英文；PR/issue 正文英文，`Closes #N` 关联；无 AI 署名 |
| 收尾 | PR 合并 → `git worktree remove` + `git branch -d` |
