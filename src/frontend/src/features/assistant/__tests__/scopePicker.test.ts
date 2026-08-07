import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';

/* ============================================================
   输入框上方那一行：加号 + 课题选择。

   这三条都是编译器看不见的回归——把默认值改回 true、把选择器塞进加号菜单、或者
   让课题重新变成一行只读文字，类型全都照样过。所以在源码层面钉住。
   ============================================================ */

const source = readFileSync(
  fileURLToPath(new URL('../AssistantPanel.tsx', import.meta.url)),
  'utf-8',
);

/** 从某一行开始数括号的开合，找出这一层在哪一行收口。 */
function blockEnd(lines: string[], openIndex: number, open: RegExp, close: RegExp): number {
  let depth = 0;
  for (let i = openIndex; i < lines.length; i += 1) {
    const line = lines[i]!;
    depth += (line.match(open) ?? []).length;
    depth -= (line.match(close) ?? []).length;
    if (i > openIndex && depth === 0) return i;
  }
  return -1;
}

describe('会话侧栏的默认状态', () => {
  it('默认收起：只有存过 1 才展开', () => {
    // 打开 Buddy 是为了问一句话，不是翻旧账；两栏一起铺开会把对话区再切一刀。
    expect(source).toContain("localStorage.getItem('polaris.buddyRailOpen') === '1'");
    // 反面：别再出现无条件展开的初值
    expect(source).not.toContain('useState(true);\n  const [historyOpen');
  });

  it('手动开合会被记住', () => {
    expect(source).toContain("localStorage.setItem('polaris.buddyRailOpen'");
  });
});

describe('课题选择', () => {
  const lines = source.split('\n');
  const plusButton = lines.findIndex((l) => l.includes('setPlusOpen((o) => !o)'));
  const scopeButton = lines.findIndex((l) => l.includes('className="buddy-scope"'));

  it('是一枚按钮，和加号并排在输入框上方', () => {
    expect(plusButton).toBeGreaterThan(0);
    expect(scopeButton).toBeGreaterThan(plusButton);
    // 中间不能隔着输入框：两者必须在同一行容器里
    const composer = lines.findIndex((l) => l.includes('padding: 12, borderTop:'));
    expect(composer).toBeGreaterThan(0);
    expect(plusButton).toBeGreaterThan(composer);
  });

  it('加号菜单里不再有「只查课题」——那件事归选择器了', () => {
    const menuStart = lines.findIndex((l) => l.includes('{plusOpen && ('));
    expect(menuStart).toBeGreaterThan(0);
    const menuEnd = blockEnd(lines, menuStart, /\(/g, /\)/g);
    expect(menuEnd).toBeGreaterThan(menuStart);
    const menu = lines.slice(menuStart, menuEnd).join('\n');
    expect(menu).not.toContain('setTopicId');
    expect(menu).not.toContain('只查课题');
  });

  it('选中的课题就是这一轮问话的作用域', () => {
    // 以前是「绑不绑当前课题」的开关，只能绑你正在看的那个；现在存的是选中的 id
    expect(source).toContain('projectId: topicId,');
    expect(source).not.toContain('bindProject');
  });

  it('载入历史会话时采用它自己的课题', () => {
    // 这句注释以前是空头支票：写着「跟着切」，却没有一行代码在切
    expect(source).toContain('pickTopic(conversations.find((c) => c.id === id)?.project_id ?? null)');
  });

  it('默认跟随你正在做的那个课题', () => {
    // 问「实验跑得怎么样」，指的多半是眼前这个课题的实验，不是全实验室的
    expect(source).toContain('useState<string | null>(currentProjectId)');
    expect(source).toContain('if (!topicPinned.current) setTopicId(currentProjectId)');
  });

  it('用户自己选过之后就不再被界面切换带着走', () => {
    // 否则他在 Buddy 里挑了 A，回头在侧栏切到 B，问话范围会被悄悄换掉
    expect(source).toContain('topicPinned.current = true');
  });
});

