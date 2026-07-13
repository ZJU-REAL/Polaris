export interface StatusMeta {
  cls: string;
  zh: string;
  en: string;
}

export const STATUS: Record<string, StatusMeta> = {
  candidate: { cls: 'st-candidate', zh: '候选', en: 'candidate' },
  accepted: { cls: 'st-accepted', zh: '已采纳', en: 'accepted' },
  implemented: { cls: 'st-implemented', zh: '已实验', en: 'implemented' },
  drafted: { cls: 'st-drafted', zh: '初稿', en: 'drafted' },
  reviewed: { cls: 'st-reviewed', zh: '已评审', en: 'reviewed' },
  rejected: { cls: 'st-rejected', zh: '已淘汰', en: 'rejected' },
  running: { cls: 'st-running', zh: '运行中', en: 'running' },
  done: { cls: 'st-implemented', zh: '完成', en: 'done' },
  summarized: { cls: 'st-implemented', zh: '已综合', en: 'summarized' },
  ingested: { cls: 'st-drafted', zh: '已收录', en: 'ingested' },
  planned: { cls: 'st-candidate', zh: '已规划', en: 'planned' },
  pending: { cls: 'st-candidate', zh: '待审批', en: 'pending' },
  approved: { cls: 'st-implemented', zh: '已批准', en: 'approved' },
  // —— Paper 状态（M2 论文库） ——
  scored: { cls: 'st-accepted', zh: '已打分', en: 'scored' },
  fetched: { cls: 'st-drafted', zh: '已抓取', en: 'fetched' },
  compiled: { cls: 'st-reviewed', zh: '已编译', en: 'compiled' },
  included: { cls: 'st-implemented', zh: '已纳入', en: 'included' },
  excluded: { cls: 'st-rejected', zh: '已排除', en: 'excluded' },
  // —— Voyage 状态机 ——
  planning: { cls: 'st-drafted', zh: '规划中', en: 'planning' },
  executing: { cls: 'st-running', zh: '执行中', en: 'executing' },
  verifying: { cls: 'st-reviewed', zh: '自检中', en: 'verifying' },
  replanning: { cls: 'st-drafted', zh: '重规划', en: 'replanning' },
  paused_gate: { cls: 'st-candidate', zh: '等待审批', en: 'paused' },
  paused_error: { cls: 'st-failed', zh: '出错暂停', en: 'error' },
  failed: { cls: 'st-failed', zh: '失败', en: 'failed' },
  cancelled: { cls: 'st-rejected', zh: '已取消', en: 'cancelled' },
  // —— Project 状态 ——
  active: { cls: 'st-implemented', zh: '进行中', en: 'active' },
  draft: { cls: 'st-candidate', zh: '草稿', en: 'draft' },
  archived: { cls: 'st-rejected', zh: '已归档', en: 'archived' },
};

export interface StatusPillProps {
  status: string;
  sm?: boolean;
}

export function StatusPill({ status, sm }: StatusPillProps) {
  const s = STATUS[status] ?? { cls: '', zh: status, en: status };
  return (
    <span className={`pill ${s.cls}${sm ? ' sm' : ''}`}>
      <span className="dot" />
      {s.zh}
      <span style={{ opacity: 0.55, fontWeight: 500, fontSize: '0.9em' }}>{s.en}</span>
    </span>
  );
}
