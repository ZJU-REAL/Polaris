import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';

import { LLM_STAGES, PLUGIN_STAGE_RE, isPluginStage } from '../api';
import { STAGE_LABELS, stageLabel } from '../stageLabels';

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

   插件命名空间环节（#736）是这条铁律的唯一豁免：plugin:<pack>:<stage> 由插件
   运行时注册，天然不进两边的静态清单——守卫改为「内置精确对齐 + 命名空间
   正则两边一致」。正则也要对账：前端拿它决定哪些串按插件行渲染/随保存提交，
   后端拿它决定路由表 PUT 放不放行，两边漂了就是「界面画得出、保存 400」重演。
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

/** 从 router.py 里抠出 PLUGIN_STAGE_RE 的正则字面量。 */
const backendPluginRe = (() => {
  const m = /^PLUGIN_STAGE_RE = re\.compile\(r"([^"]+)"\)/m.exec(routerSource);
  if (!m) throw new Error('router.py 里找不到 PLUGIN_STAGE_RE——改了形状就同步改这里');
  return m[1]!;
})();

describe('LLM stage 清单', () => {
  it('后端确实被解析到了（守卫本身不能空跑）', () => {
    expect(backendStages.length).toBeGreaterThan(10);
    expect(backendStages).toContain('default');
  });

  it('内置清单前后端逐项一致', () => {
    expect([...LLM_STAGES].sort()).toEqual([...backendStages].sort());
  });

  it('每个内置环节都有大白话名字', () => {
    const missing = LLM_STAGES.filter((stage) => !STAGE_LABELS[stage]);
    expect(missing).toEqual([]);
  });

  it('内置清单里不许混入命名空间串（插件环节走运行时注册，不进静态清单）', () => {
    expect(LLM_STAGES.filter((s) => isPluginStage(s))).toEqual([]);
    expect(backendStages.filter((s) => s.startsWith('plugin:'))).toEqual([]);
  });
});

describe('插件命名空间环节（#736）', () => {
  it('命名正则与后端一字不差', () => {
    // 后端正则带捕获组、前端不带——归一化掉括号再比（两边语义都是整串校验）
    const normalize = (src: string) => src.replace(/[()]/g, '');
    expect(normalize(PLUGIN_STAGE_RE.source)).toEqual(normalize(backendPluginRe));
  });

  it('三段式、段字符集限 [a-z0-9-]', () => {
    expect(isPluginStage('plugin:pico-pack:extract-pico')).toBe(true);
    expect(isPluginStage('plugin:a1:b2')).toBe(true);
    for (const bad of [
      'plugin:pico', // 少一段
      'plugin:pico:extract:extra', // 多一段
      'plugin:PICO:extract', // 大写
      'plugin:pi_co:extract', // 下划线
      'plugin::extract', // 空段
      'extract_skeleton', // 内置环节不算
      'pluginx:pico:extract', // 前缀必须是 plugin:
    ]) {
      expect(isPluginStage(bad), bad).toBe(false);
    }
  });

  it('stageLabel 对插件串照排原串（badge 由渲染处补）', () => {
    expect(stageLabel('plugin:pico-pack:extract-pico')).toEqual({
      zh: 'plugin:pico-pack:extract-pico',
      en: 'plugin:pico-pack:extract-pico',
    });
    expect(stageLabel('default')).toEqual(STAGE_LABELS['default']);
  });
});
