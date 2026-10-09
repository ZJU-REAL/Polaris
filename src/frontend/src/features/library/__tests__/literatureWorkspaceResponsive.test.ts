import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';

const css = readFileSync(
  fileURLToPath(new URL('../../../styles/global.css', import.meta.url)),
  'utf-8',
);
const personalLibrary = readFileSync(
  fileURLToPath(new URL('../LibraryPage.tsx', import.meta.url)),
  'utf-8',
);
const projectLibrary = readFileSync(
  fileURLToPath(new URL('../../wiki/WikiPage.tsx', import.meta.url)),
  'utf-8',
);

describe('literature workspace responsive layout', () => {
  it('marks each literature workspace without changing shared split behavior', () => {
    for (const source of [personalLibrary, projectLibrary]) {
      expect(source).toContain('literature-workspace-tabs');
      expect(source).toContain('literature-workspace-card');
    }
  });

  it('uses the main content container and preserves complete tab labels', () => {
    // 1440 窗口下主区约 1170px：列表与详情必须仍然并排（与每日新论文一致），
    // 只有主区窄于 900px 才堆叠。
    const stackRule = '.literature-workspace-card .split { flex-direction: column; }';
    const at900 = css.indexOf('@container mainarea (max-width: 900px)');
    const at1180 = css.indexOf('@container mainarea (max-width: 1180px)');
    expect(at900).toBeGreaterThan(-1);
    expect(css.indexOf(stackRule)).toBeGreaterThan(at900);
    expect(css.indexOf(stackRule, at1180)).toBeGreaterThan(at900);
    expect(css.indexOf(stackRule)).toBeLessThan(css.indexOf('@container mainarea (max-width: 460px)'));
    expect(css.split(stackRule)).toHaveLength(2);
    expect(css).toMatch(/\.literature-workspace-tabs \.segmented > button[\s\S]*white-space: nowrap/);
  });
  it('declares the container the queries name', () => {
    // @container 规则找不到同名容器时不会报错，只是永远不生效：布局照旧、
    // 控制台干净、测试全绿。container-name 和 container-type 必须成对存在。
    expect(css).toMatch(/container-type:\s*inline-size/);
    expect(css).toMatch(/container-name:\s*mainarea/);
    const queried = [...css.matchAll(/@container\s+([A-Za-z-]+)\s/g)].map((m) => m[1]);
    expect(new Set(queried)).toEqual(new Set(['mainarea']));
  });
});
