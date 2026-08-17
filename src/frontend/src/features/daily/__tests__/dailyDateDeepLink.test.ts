import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';

/* ============================================================
   实验室工作台的每日新论文柱子 → 直达那一天。

   两边各有一半，缺一半就没用：工作台不带 ?date= 是白链，/daily 不认 ?date= 是白传。
   而两种坏法的表现一模一样——点“8月4日 · 903 篇”落在最新一天，看着像没生效。
   类型检查看不见这类回归，所以在源码层面钉住。
   ============================================================ */

const read = (rel: string) => readFileSync(fileURLToPath(new URL(rel, import.meta.url)), 'utf-8');

/** 只看代码：注释里正好在解释这件事，别把它当成实现。 */
const code = (source: string) =>
  source
    .replace(/\/\*[\s\S]*?\*\//g, '')
    .replace(/\{\/\*[\s\S]*?\*\/\}/g, '')
    .replace(/(^|[^:])\/\/.*$/gm, '$1');

const lab = code(read('../../lab/LabPage.tsx'));
const daily = code(read('../DailyPage.tsx'));

describe('每日新论文的日期直达', () => {
  it('工作台的柱子带上 ?date=', () => {
    expect(lab).toMatch(/to=\{`\/daily\?date=\$\{encodeURIComponent\(d\.date\)\}`\}/);
  });

  it('版块标题旁的按钮不带日期（那是「去看看」，不是某一天）', () => {
    expect(lab).toContain('to="/daily"');
  });

  it('/daily 认 ?date=', () => {
    expect(daily).toContain('useSearchParams');
    expect(daily).toMatch(/searchParams\.get\('date'\)/);
  });

  it('日期初值直接取 URL，不只靠 effect', () => {
    // 只走 effect 的话，「默认停在最新一天」那个 effect 会先跑，
    // 页面会先闪一下最新那天再跳到目标日期。
    expect(daily).toMatch(/useState\(\(\) => searchParams\.get\('date'\) \?\? ''\)/);
  });

  it('用过就把参数清掉，且不往历史里塞记录', () => {
    // 留着的话，页内切到别的日期再刷新会被它按回去。
    expect(daily).toMatch(/setSearchParams\(\{\}, \{ replace: true \}\)/);
  });
});
