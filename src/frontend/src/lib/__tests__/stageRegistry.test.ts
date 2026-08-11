import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';

import { LLM_STAGES } from '../api';
import { STAGE_LABELS } from '../stageLabels';

/* ============================================================
   前端的 stage 清单必须和后端 router.py 的 STAGES 逐项一致。

   这是类型检查永远看不见的一类回归，而且两个方向都真的坏过：

   - 前端多一个后端没有的（`digest` 就是这样）：设置页照常把那一行画出来，管理员
     一配就 400 `unknown stage`。而 PUT 是**整表覆盖**，所以挂掉的不是那一行，是
     整张路由表——界面只报「保存失败」，没人会想到是某个环节名字对不上。
     它躲了很久没被发现，因为那时 digest 在后端偷偷继承 librarian，跑起来「像是好的」。

   - 后端多一个前端没有的（`agent` 就是这样）：那个环节在界面上根本不存在，
     配不了也看不见，只能直接改数据库。

   后端没有跑测试的 CI 工作流，前端有；而 vitest 跑在 node 里读得到后端源码。
   所以这条守卫放在这边。
   ============================================================ */

const routerSource = readFileSync(
  fileURLToPath(new URL('../../../../backend/app/core/llm/router.py', import.meta.url)),
  'utf-8',
);

/** 从 router.py 里抠出 STAGES 元组的字面量成员。 */
const backendStages = (() => {
  const body = /^STAGES = \(([\s\S]*?)^\)/m.exec(routerSource)?.[1];
  if (!body) throw new Error('router.py 里找不到 STAGES 元组——改了形状就同步改这里');
  return [...body.matchAll(/"([a-z_]+)"/g)].map((m) => m[1]!);
})();

describe('LLM stage 清单', () => {
  it('后端确实被解析到了（守卫本身不能空跑）', () => {
    expect(backendStages.length).toBeGreaterThan(10);
    expect(backendStages).toContain('default');
  });

  it('前后端逐项一致', () => {
    expect([...LLM_STAGES].sort()).toEqual([...backendStages].sort());
  });

  it('每个环节都有大白话名字', () => {
    const missing = LLM_STAGES.filter((stage) => !STAGE_LABELS[stage]);
    expect(missing).toEqual([]);
  });
});
