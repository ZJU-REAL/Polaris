/* ============================================================
   PolarisBuddy 的页面感知：从当前路由推「用户正在看什么」。

   感知完全在前端做——Buddy 挂在 AppShell 里，本来就能读路由。发问时把这份上下文
   作为 page_context 传给后端注入本轮消息，**不新增任何后端感知机制**：上下文由前端
   声明，天然不越权（Buddy 拿着 id 去调工具读内容时，走的还是原有的权限口）。
   ============================================================ */

export interface PageContext {
  kind: 'paper' | 'idea' | 'experiment' | 'library' | 'project' | 'daily' | 'manuscript';
  id?: string;
  /** 人读的一句话，注入提示词用（"用户正在读论文 …"） */
  label: string;
}

const PATTERNS: [RegExp, (m: RegExpMatchArray) => PageContext][] = [
  [
    /^\/papers\/([0-9a-f-]{36})\/read/,
    (m) => ({ kind: 'paper', id: m[1], label: '正在读一篇论文' }),
  ],
  [/^\/ideas\/([0-9a-f-]{36})/, (m) => ({ kind: 'idea', id: m[1], label: '正在看一个研究想法' })],
  [
    /^\/experiment\/([0-9a-f-]{36})/,
    (m) => ({ kind: 'experiment', id: m[1], label: '正在看一个实验' }),
  ],
  [
    /^\/libraries\/([0-9a-f-]{36})/,
    (m) => ({ kind: 'library', id: m[1], label: '正在浏览一个文献库' }),
  ],
  [/^\/writer\/([0-9a-f-]{36})/, (m) => ({ kind: 'manuscript', id: m[1], label: '正在写论文稿' })],
  [/^\/t\/([0-9a-f-]{36})/, (m) => ({ kind: 'project', id: m[1], label: '正在课题工作台' })],
  [/^\/daily/, () => ({ kind: 'daily', label: '正在看每日新论文' })],
];

/** 从路由 pathname 推页面上下文；认不出返回 null（Buddy 照常工作，只是不带上下文）。 */
export function pageContextFrom(pathname: string): PageContext | null {
  for (const [pattern, build] of PATTERNS) {
    const m = pathname.match(pattern);
    if (m) return build(m);
  }
  return null;
}
