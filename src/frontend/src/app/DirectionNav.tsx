import { useEffect, useRef, useState, type ReactNode } from 'react';
import { Link, useLocation } from 'react-router-dom';
import { Icon } from '../components/ui/Icon';
import { useDirections, type Direction } from '../features/deepresearch/directions';
import { runHeadline } from '../features/deepresearch/phase';
import { StatusDot } from '../features/voyages/shared/StatusDot';
import { tr } from '../lib/i18n';
import { topicPath } from './project';
import './DirectionNav.css';

/* ============================================================
   课题页头：课题名 + 一排页签。一个课题的所有页面（概览 / 想法 / 实验 / 写作 /
   评审 / 相关研究 / 任务 / 设置）都顶着这同一个页头，所以它们读起来是「同一个课题
   里的几页」，而不是侧栏上互不相干的几个功能。侧栏只列课题，不再列这些页面。

   常用的四页平铺，其余收在「更多」里；正在看的那一页如果在「更多」里，
   就临时多出一个页签，保证当前位置始终看得见。
   ============================================================ */

export type TopicTabKey =
  | 'overview'
  | 'forge'
  | 'experiment'
  | 'writer'
  | 'review'
  | 'paper-review'
  | 'research'
  | 'tasks'
  | 'settings';

interface TabDef {
  key: TopicTabKey;
  /** /t/<id>/ 后面的那一段；概览没有 */
  sub?: string;
  zh: string;
  en: string;
  /** 平铺还是收在「更多」里 */
  main: boolean;
}

/** 页签的唯一一份定义：面包屑的最后一段也从这里取名字，两处永远对得上。 */
export const TOPIC_TABS: readonly TabDef[] = [
  { key: 'overview', zh: '概览', en: 'Overview', main: true },
  { key: 'forge', sub: 'forge', zh: '想法', en: 'Ideas', main: true },
  { key: 'experiment', sub: 'experiment', zh: '实验', en: 'Experiments', main: true },
  { key: 'writer', sub: 'writer', zh: '写作', en: 'Writing', main: true },
  { key: 'review', sub: 'review', zh: '想法对比', en: 'Idea comparisons', main: false },
  { key: 'paper-review', sub: 'paper-review', zh: '稿件评审', en: 'Manuscript review', main: false },
  { key: 'research', sub: 'research', zh: '相关研究', en: 'Related work', main: false },
  { key: 'tasks', sub: 'tasks', zh: '任务', en: 'Tasks', main: false },
  { key: 'settings', sub: 'settings', zh: '设置', en: 'Settings', main: false },
];

export function topicTabLabel(key: TopicTabKey): string {
  const t = TOPIC_TABS.find((x) => x.key === key);
  return t ? tr(t.zh, t.en) : '';
}

/** 当前路径是课题的哪一页；不是课题页返回 null。 */
export function topicTabOf(pathname: string): TopicTabKey | null {
  if (/^\/research\/[^/]+$/.test(pathname)) return 'overview';
  const m = /^\/t\/[^/]+(?:\/([^/]+))?\/?$/.exec(pathname);
  if (!m) return null;
  if (!m[1]) return 'overview';
  const t = TOPIC_TABS.find((x) => x.sub === m[1]);
  return t ? t.key : null;
}

function tabHref(d: Direction, tab: TabDef): string {
  return tab.sub ? topicPath(d.projectId, tab.sub) : d.href;
}

export interface DirectionHeaderProps {
  projectId: string;
  /** 课题名的兜底（列表还没取到时用页面自己知道的名字） */
  fallbackName?: string;
  /** 标题行右侧的操作（时间线页的「更多」菜单） */
  actions?: ReactNode;
}

export function DirectionHeader({ projectId, fallbackName, actions }: DirectionHeaderProps) {
  const location = useLocation();
  const { byProject } = useDirections();
  const direction: Direction =
    byProject(projectId) ?? { projectId, name: fallbackName ?? '', run: null, href: topicPath(projectId), updatedAt: '' };
  const active = topicTabOf(location.pathname);
  const head = direction.run ? runHeadline(direction.run) : null;

  const [moreOpen, setMoreOpen] = useState(false);
  const moreRef = useRef<HTMLDivElement | null>(null);
  useEffect(() => setMoreOpen(false), [location.pathname]);
  useEffect(() => {
    if (!moreOpen) return;
    const onDown = (e: MouseEvent) => {
      if (moreRef.current && !moreRef.current.contains(e.target as Node)) setMoreOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setMoreOpen(false);
    };
    document.addEventListener('mousedown', onDown);
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('mousedown', onDown);
      document.removeEventListener('keydown', onKey);
    };
  }, [moreOpen]);

  const shown = TOPIC_TABS.filter((t) => t.main || t.key === active);
  const hidden = TOPIC_TABS.filter((t) => !t.main && t.key !== active);

  return (
    <header className="dn">
      <div className="dn-top">
        <h1 className="dn-title" title={direction.name}>
          {direction.name || tr('课题', 'Topic')}
        </h1>
        {head && <StatusDot tone={head.tone} label={head.label} />}
        {actions && <div className="dn-actions">{actions}</div>}
      </div>
      <nav className="dn-tabs" aria-label={tr('课题的页面', 'Pages of this topic')}>
        {shown.map((t) => (
          <Link
            key={t.key}
            to={tabHref(direction, t)}
            className={'dn-tab' + (t.key === active ? ' is-active' : '')}
            aria-current={t.key === active ? 'page' : undefined}
          >
            {tr(t.zh, t.en)}
          </Link>
        ))}
        {hidden.length > 0 && (
          <div className="dn-more" ref={moreRef}>
            <button
              type="button"
              className={'dn-tab' + (moreOpen ? ' is-open' : '')}
              aria-haspopup="menu"
              aria-expanded={moreOpen}
              onClick={() => setMoreOpen((o) => !o)}
            >
              {tr('更多', 'More')}
              <Icon name="chevDown" size={11} style={{ transform: moreOpen ? 'rotate(180deg)' : undefined }} />
            </button>
            {moreOpen && (
              <div className="dn-menu" role="menu">
                {hidden.map((t) => (
                  <Link key={t.key} role="menuitem" className="user-menu-item" to={tabHref(direction, t)}>
                    {tr(t.zh, t.en)}
                  </Link>
                ))}
              </div>
            )}
          </div>
        )}
      </nav>
    </header>
  );
}
