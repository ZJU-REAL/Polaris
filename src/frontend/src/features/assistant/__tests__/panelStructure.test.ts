import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';

/* ============================================================
   面板的骨架结构。

   这条测试的由来：修 JSX 时我把输入区留在了「会话列表 + 消息区」那个横向 row 里面，
   于是它变成并排的第三列——整块界面塌成三条窄柱、中文被压成竖排单字。**编译器对此
   一无所知**：标签是配平的，类型也没错，只是嵌到了错的层里。

   所以这里不测渲染结果（jsdom 也测不出观感），而是把「输入区必须是 row 的兄弟、
   不是它的孩子」这一条用结构断言钉住。
   ============================================================ */

// fileURLToPath 而不是 .pathname：路径里有中文，pathname 是百分号编码的，
// 直接拿去 open 会 ENOENT
const source = readFileSync(
  fileURLToPath(new URL('../AssistantPanel.tsx', import.meta.url)),
  'utf-8',
);

/** 从某一行开始数 div 的开合，找出这一层在哪一行收口。

    **不按缩进判**：这份文件里滚动区和外层 row 的收口恰好同为 6 个空格，按缩进会
    把里层的收口误认成外层的——反向验证时就是这么让一条坏结构照样通过的。 */
function closingLineOf(lines: string[], openIndex: number): number {
  let depth = 0;
  for (let i = openIndex; i < lines.length; i += 1) {
    const line = lines[i]!;
    depth += (line.match(/<div\b/g) ?? []).length;
    depth -= (line.match(/<\/div>/g) ?? []).length;
    if (i > openIndex && depth === 0) return i;
  }
  return -1;
}

describe('面板骨架', () => {
  const lines = source.split('\n');
  const rowIndex = lines.findIndex((l) => l.includes("className=\"row\" style={{ flex: 1, minHeight: 0"));
  const composerIndex = lines.findIndex((l) => l.includes("padding: 12, borderTop:"));

  it('会话列表与消息区在同一个横向 row 里', () => {
    expect(rowIndex).toBeGreaterThan(0);
    const railIndex = lines.findIndex((l) => l.includes('<ConversationRail'));
    const scrollIndex = lines.findIndex((l) => l.includes('className="buddy-scroll"'));
    const rowEnd = closingLineOf(lines, rowIndex);
    expect(rowEnd).toBeGreaterThan(0);
    expect(railIndex).toBeGreaterThan(rowIndex);
    expect(railIndex).toBeLessThan(rowEnd);
    expect(scrollIndex).toBeLessThan(rowEnd);
  });

  it('输入区在 row 之外——在里面就会变成并排的第三列', () => {
    const rowEnd = closingLineOf(lines, rowIndex);
    // 先断言真的找到了收口：找不到时返回 -1，下面那句「大于 -1」永远成立，
    // 测试会在结构最坏的时候（row 根本没关）反而通过。反向验证就是这么发现的。
    expect(rowEnd).toBeGreaterThan(rowIndex);
    expect(composerIndex).toBeGreaterThan(rowEnd);
  });
});
