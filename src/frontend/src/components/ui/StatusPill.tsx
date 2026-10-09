import { tr } from '../../lib/i18n';

export interface StatusMeta {
  cls: string;
  zh: string;
  en: string;
}

export const STATUS: Record<string, StatusMeta> = {
  candidate: { cls: 'st-candidate', zh: '候选', en: 'Candidate' },
  accepted: { cls: 'st-accepted', zh: '已采纳', en: 'Accepted' },
  implemented: { cls: 'st-implemented', zh: '已实验', en: 'Tested' },
  drafted: { cls: 'st-drafted', zh: '初稿', en: 'Draft' },
  reviewed: { cls: 'st-reviewed', zh: '已评审', en: 'Reviewed' },
  rejected: { cls: 'st-rejected', zh: '已淘汰', en: 'Rejected' },
  // —— Idea 状态（M3 Idea Forge / Review） ——
  under_review: { cls: 'st-reviewed', zh: '评审中', en: 'In review' },
  promoted: { cls: 'st-implemented', zh: '已晋级', en: 'Promoted' },
  running: { cls: 'st-running', zh: '运行中', en: 'Running' },
  done: { cls: 'st-implemented', zh: '已完成', en: 'Done' },
  summarized: { cls: 'st-implemented', zh: '已汇总', en: 'Synthesized' },
  ingested: { cls: 'st-drafted', zh: '已收录', en: 'Added' },
  planned: { cls: 'st-candidate', zh: '已规划', en: 'Planned' },
  pending: { cls: 'st-candidate', zh: '待审批', en: 'Pending' },
  approved: { cls: 'st-implemented', zh: '已批准', en: 'Approved' },
  // —— Paper 状态（M2 论文库） ——
  scored: { cls: 'st-accepted', zh: '已打分', en: 'Scored' },
  fetched: { cls: 'st-drafted', zh: '已抓取', en: 'Fetched' },
  compiled: { cls: 'st-reviewed', zh: '已解读', en: 'Summarized' },
  included: { cls: 'st-implemented', zh: '已加入', en: 'Added' },
  excluded: { cls: 'st-rejected', zh: '已删除', en: 'Removed' },
  // —— Voyage 状态机 ——
  planning: { cls: 'st-drafted', zh: '规划中', en: 'Planning' },
  executing: { cls: 'st-running', zh: '执行中', en: 'Running' },
  verifying: { cls: 'st-reviewed', zh: '检查中', en: 'Checking' },
  replanning: { cls: 'st-drafted', zh: '调整计划', en: 'Replanning' },
  paused_gate: { cls: 'st-candidate', zh: '等待审批', en: 'Awaiting approval' },
  paused_error: { cls: 'st-failed', zh: '出错暂停', en: 'Paused on error' },
  paused_ask: { cls: 'st-candidate', zh: '等你回复', en: 'Needs your reply' },
  failed: { cls: 'st-failed', zh: '失败', en: 'Failed' },
  cancelled: { cls: 'st-rejected', zh: '已取消', en: 'Cancelled' },
  // —— Voyage 步骤（任务板，docs/task-system.md §7（原 voyage-loop.md §4）） ——
  passed: { cls: 'st-implemented', zh: '通过', en: 'Passed' },
  obsolete: { cls: 'st-rejected', zh: '已作废', en: 'Obsolete' },
  // —— Experiment 状态（M4 Experiment Lab） ——
  awaiting_gate: { cls: 'st-candidate', zh: '等待审批', en: 'Awaiting approval' },
  setup: { cls: 'st-drafted', zh: '准备环境', en: 'Setting up' },
  waiting_user: { cls: 'st-candidate', zh: '等你回复', en: 'Needs your reply' },
  reporting: { cls: 'st-reviewed', zh: '写报告中', en: 'Writing report' },
  succeeded: { cls: 'st-implemented', zh: '成功', en: 'Succeeded' },
  // —— Manuscript 状态（M5-B Paper Writer） ——
  drafting: { cls: 'st-running', zh: '起草中', en: 'Drafting' },
  writing: { cls: 'st-running', zh: '起草中', en: 'Drafting' },
  submitted: { cls: 'st-implemented', zh: '已投稿', en: 'Submitted' },
  // —— Project 状态 ——
  active: { cls: 'st-implemented', zh: '进行中', en: 'Active' },
  draft: { cls: 'st-candidate', zh: '草稿', en: 'Draft' },
  archived: { cls: 'st-rejected', zh: '已归档', en: 'Archived' },
};

/** 论文三态（docs/task-system.md §7 大白话）：检索到 = 已抓取；相关性达标 = 已纳入；编译完成 = 已编译。
    与全局 STATUS 分开：candidate 等键在 Idea 等场景有不同含义。 */
const PAPER_STATUS: Record<string, StatusMeta> = {
  candidate: { cls: 'st-drafted', zh: '已抓取', en: 'Fetched' },
  scored: { cls: 'st-accepted', zh: '已加入', en: 'Added' },
  fetched: { cls: 'st-accepted', zh: '已加入', en: 'Added' },
  included: { cls: 'st-accepted', zh: '已加入', en: 'Added' },
  compiled: { cls: 'st-implemented', zh: '已解读', en: 'Summarized' },
  excluded: { cls: 'st-rejected', zh: '已删除', en: 'Removed' },
};

/** 状态色调 → .status 圆点颜色（见 global.css）。 */
export type StatusTone = 'ok' | 'warn' | 'err' | 'info' | 'accent' | 'violet' | 'idle' | 'running';

const CLS_TONE: Record<string, StatusTone> = {
  'st-candidate': 'warn',
  'st-accepted': 'accent',
  'st-implemented': 'ok',
  'st-drafted': 'info',
  'st-reviewed': 'violet',
  'st-rejected': 'idle',
  'st-running': 'running',
  'st-failed': 'err',
};

export interface StatusDotProps {
  tone: StatusTone;
  label: string;
  sm?: boolean;
  title?: string;
}

/** 8px 圆点 + 一个词：列表里表示状态的默认写法（不用彩色底块）。 */
export function StatusDot({ tone, label, sm, title }: StatusDotProps) {
  return (
    <span className={`status tone-${tone}${sm ? ' sm' : ''}`} title={title}>
      <span className="status-dot" />
      {label}
    </span>
  );
}

function render(s: StatusMeta, sm?: boolean, badge?: boolean) {
  if (badge) {
    return (
      <span className={`pill ${s.cls}${sm ? ' sm' : ''}`}>
        <span className="dot" />
        {tr(s.zh, s.en)}
      </span>
    );
  }
  return <StatusDot tone={CLS_TONE[s.cls] ?? 'idle'} label={tr(s.zh, s.en)} sm={sm} />;
}

/** 论文状态：把内部六种 status 折叠为用户可见三态。

    ``hasWiki`` 优先于 status：**「编译过没有」是论文级的事实**（解读存在 paper_wikis
    上，全平台一份），而 status 是成员行上的判断。人工纳入的论文状态一直是 included，
    编译完也不会变——只看 status 的话，明明已经编译出解读的论文在列表里仍写着「已纳入」。
    以事实为准，别让用户去分辨两套口径。 */
export function PaperStatusPill({ status, hasWiki, sm, badge }: StatusPillProps) {
  const effective = hasWiki && status !== 'excluded' ? 'compiled' : status;
  return render(PAPER_STATUS[effective] ?? { cls: '', zh: status, en: status }, sm, badge);
}

export interface StatusPillProps {
  /** 这篇有没有编译出解读（论文级事实，优先于成员行 status） */
  hasWiki?: boolean;
  status: string;
  sm?: boolean;
  /** 画成彩色底块（旧样式）。默认是圆点 + 文字；只在一行里需要特别显眼时用。 */
  badge?: boolean;
}

/** 状态：默认圆点 + 文字。 */
export function StatusPill({ status, sm, badge }: StatusPillProps) {
  return render(STATUS[status] ?? { cls: '', zh: status, en: status }, sm, badge);
}
