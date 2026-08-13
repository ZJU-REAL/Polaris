import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';

/* ============================================================
   每日新论文列表：滚到底自动加载，不用「上一页 / 下一页」。

   这类回归类型检查看不见——把 useInfiniteQuery 换回 useQuery、或者把哨兵删掉，
   编译一样过，只有真去滚那个列表才会发现加载不出来。所以在源码层面钉住。
   ============================================================ */

const source = readFileSync(
  fileURLToPath(new URL('../DailyPage.tsx', import.meta.url)),
  'utf-8',
);

/** 只看代码：注释里正好在解释「不做上一页/下一页」，别把它当成还留着那些按钮。 */
const code = source
  .replace(/\/\*[\s\S]*?\*\//g, '')
  .replace(/\{\/\*[\s\S]*?\*\/\}/g, '')
  .replace(/(^|[^:])\/\/.*$/gm, '$1');

describe('每日列表的加载方式', () => {
  it('用 useInfiniteQuery 累积，不是按页替换', () => {
    expect(code).toContain('useInfiniteQuery');
    expect(code).toContain('getNextPageParam');
    // 多页结果要拼起来，而不是只显示当前页
    expect(code).toMatch(/pages\s*\?\?\s*\[\]/);
    expect(code).toContain('flatMap');
  });

  it('没有翻页按钮，也没有页码状态', () => {
    expect(code).not.toContain('上一页');
    expect(code).not.toContain('下一页');
    expect(code).not.toMatch(/setPage\s*\(/);
  });

  it('靠 IntersectionObserver 触底加载，而不是监听 scroll 自己算阈值', () => {
    expect(code).toContain('IntersectionObserver');
    expect(code).toContain('sentinelRef');
    // 已经在取下一页时不能重复触发
    expect(code).toContain('isFetchingNextPage');
  });
});
