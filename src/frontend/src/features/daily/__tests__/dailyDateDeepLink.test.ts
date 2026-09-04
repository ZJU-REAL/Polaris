import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';

/* ============================================================
   /daily 认 ?date= 深链。站内发这种链接的柱状图随实验室面板一起移除了
   （#626），但 URL 契约保留：收藏/外部分享的带日期链接还要能直达那一天。
   坏掉的表现是「带着日期进来却落在最新一天」，类型检查看不见这类回归，
   所以在源码层面钉住。
   ============================================================ */

const read = (rel: string) => readFileSync(fileURLToPath(new URL(rel, import.meta.url)), 'utf-8');

/** 只看代码：注释里正好在解释这件事，别把它当成实现。 */
const code = (source: string) =>
  source
    .replace(/\/\*[\s\S]*?\*\//g, '')
    .replace(/\{\/\*[\s\S]*?\*\/\}/g, '')
    .replace(/(^|[^:])\/\/.*$/gm, '$1');

const daily = code(read('../DailyPage.tsx'));

describe('每日新论文的日期直达', () => {
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
