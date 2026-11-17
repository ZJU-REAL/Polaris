import { existsSync, readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';

import { STAGE_LABELS, stageLabel } from '../stageLabels';

/* ============================================================
   环节（stage）的大白话名字。

   模型固定为一个之后，前端不再有「给每个环节配模型」的路由表，也就不再需要一份和
   后端逐项对齐的 stage 清单。stage 现在只出现在两处：用量明细和调用记录的「环节」
   一列——少一个名字，那一格就显示成 `rag_expand` 这样的内部标识符。

   所以只要后端 router.py 里还有 STAGES 元组，就检查每一项都有名字；后端把这份
   清单挪走或删掉时这一条跳过（不拦路），stageLabel 的兜底照常保证不出空白。
   ============================================================ */

const routerPath = fileURLToPath(new URL('../../../../backend/app/core/llm/router.py', import.meta.url));

/** 从 router.py 里抠出 STAGES 元组的字面量成员；文件或元组不在了就返回 null。 */
const backendStages = (() => {
  if (!existsSync(routerPath)) return null;
  const body = /^STAGES = \(([\s\S]*?)^\)/m.exec(readFileSync(routerPath, 'utf-8'))?.[1];
  if (!body) return null;
  return [...body.matchAll(/"([a-z_]+)"/g)].map((m) => m[1]!);
})();

describe('stage labels', () => {
  it.skipIf(backendStages === null)('every backend stage has a plain name', () => {
    expect(backendStages!.length).toBeGreaterThan(10);
    expect(backendStages!.filter((stage) => !STAGE_LABELS[stage])).toEqual([]);
  });

  it('every label has both languages', () => {
    for (const [stage, label] of Object.entries(STAGE_LABELS)) {
      expect(label.zh, stage).toBeTruthy();
      expect(label.en, stage).toBeTruthy();
    }
  });

  it('unknown and plugin stages show their raw name instead of a blank', () => {
    expect(stageLabel('plugin:pico-pack:extract-pico')).toEqual({
      zh: 'plugin:pico-pack:extract-pico',
      en: 'plugin:pico-pack:extract-pico',
    });
    expect(stageLabel('default')).toEqual(STAGE_LABELS['default']);
  });
});
