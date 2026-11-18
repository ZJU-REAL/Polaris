import { tr } from '../lib/i18n';
import { topicTabLabel, topicTabOf } from './DirectionNav';
import { topicPath } from './project';

/* ============================================================
   当前路径 → 顶栏面包屑。层级和侧栏一致：
     研究 › 课题名 › 页签 › 具体条目
     论文 › 文献库名 / 我的文献库 / 论文
     每日新论文 · 任务与待办 · 设置
   纯函数：需要的名字（课题名、文献库名）由壳层从缓存里取了传进来。
   ============================================================ */

/** 面包屑一段；没有 to = 当前页（最后一段），不可点。 */
export interface Crumb {
  label: string;
  to?: string;
}

export interface CrumbContext {
  /** 这一页属于哪个课题（课题页取 URL 里的；详情页取当前课题）。没有为 null */
  topic: { id: string; name: string; href: string } | null;
  /** 文献库详情页的库名 */
  libraryName?: string | null;
  /** 任务详情：这个任务不属于任何课题（文献库 / 每日新论文的任务） */
  taskOutsideTopic?: boolean;
}

export const NAV_LABELS = {
  research: { zh: '研究', en: 'Research' },
  papers: { zh: '论文', en: 'Papers' },
  daily: { zh: '每日新论文', en: 'Daily papers' },
  activity: { zh: '任务与待办', en: 'Activity' },
  settings: { zh: '设置', en: 'Settings' },
} as const;

export type NavKey = keyof typeof NAV_LABELS;

export function navLabel(key: NavKey): string {
  return tr(NAV_LABELS[key].zh, NAV_LABELS[key].en);
}

/** 当前路径属于侧栏的哪一项（决定哪一项高亮）。 */
export function navSectionOf(pathname: string, taskOutsideTopic = false): NavKey | null {
  if (pathname === '/research' || pathname.startsWith('/research/')) return 'research';
  if (pathname.startsWith('/t/') || pathname.startsWith('/projects/')) return 'research';
  if (/^\/(ideas|experiment|writer)\//.test(pathname)) return 'research';
  if (pathname.startsWith('/voyages/')) return taskOutsideTopic ? 'activity' : 'research';
  if (pathname.startsWith('/libraries') || pathname === '/library') return 'papers';
  if (pathname.startsWith('/papers/') || pathname.startsWith('/concepts/')) return 'papers';
  if (pathname === '/daily') return 'daily';
  if (pathname === '/activity') return 'activity';
  if (pathname === '/settings') return 'settings';
  return null;
}

export function crumbsFor(pathname: string, ctx: CrumbContext): Crumb[] {
  const research: Crumb = { label: navLabel('research'), to: '/research' };
  const papers: Crumb = { label: navLabel('papers'), to: '/libraries' };
  const topic: Crumb | null = ctx.topic ? { label: ctx.topic.name || tr('课题', 'Topic'), to: ctx.topic.href } : null;
  const tab = (key: Parameters<typeof topicTabLabel>[0]): Crumb => ({
    label: topicTabLabel(key),
    to: ctx.topic ? topicPath(ctx.topic.id, key) : undefined,
  });
  const inTopic = (...rest: Crumb[]): Crumb[] => (topic ? [research, topic, ...rest] : [research, ...rest]);

  const trail = ((): Crumb[] => {
    if (pathname === '/research') return [research];
    // 课题的页面：/research/:id 是有研究进展的课题的概览，/t/:id/... 是其余各页
    const tabKey = topicTabOf(pathname);
    if (tabKey === 'overview') return inTopic();
    if (tabKey) return inTopic({ label: topicTabLabel(tabKey) });
    if (pathname.startsWith('/t/')) return inTopic();

    if (pathname.startsWith('/ideas/')) return inTopic(tab('forge'), { label: tr('想法详情', 'Idea') });
    if (pathname.startsWith('/experiment/')) return inTopic(tab('experiment'), { label: tr('实验详情', 'Experiment') });
    if (pathname.startsWith('/writer/')) return inTopic(tab('writer'), { label: tr('稿件', 'Manuscript') });
    if (pathname.startsWith('/voyages/')) {
      return ctx.taskOutsideTopic
        ? [{ label: navLabel('activity'), to: '/activity' }, { label: tr('任务详情', 'Task') }]
        : inTopic(tab('tasks'), { label: tr('任务详情', 'Task') });
    }

    if (pathname === '/libraries') return [papers, { label: tr('文献库', 'Libraries') }];
    if (pathname.startsWith('/libraries/')) return [papers, { label: ctx.libraryName || tr('文献库', 'Library') }];
    if (pathname === '/library') return [papers, { label: tr('我的文献库', 'My library') }];
    if (pathname.startsWith('/papers/')) return [papers, { label: tr('阅读', 'Reading') }];
    if (pathname.startsWith('/concepts/')) return [papers, { label: tr('概念', 'Concept') }];

    if (pathname === '/daily') return [{ label: navLabel('daily') }];
    if (pathname === '/activity') return [{ label: navLabel('activity') }];
    if (pathname === '/settings') return [{ label: navLabel('settings') }];
    return [{ label: 'Polaris' }];
  })();

  // 最后一段是当前页：去掉链接
  return trail.map((c, i) => (i === trail.length - 1 ? { label: c.label } : c));
}
