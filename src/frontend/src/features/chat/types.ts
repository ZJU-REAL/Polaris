import type { LibraryChatSource } from '../../lib/api';

/* ============================================================
   共享对话框（文献对话 / AI 伴读）类型
   ============================================================ */

export interface ChatMsg {
  role: 'user' | 'assistant';
  content: string;
  /** assistant：本次回答检索到的引用来源（仅文献对话用） */
  sources?: LibraryChatSource[];
  /** user：本条消息携带的上下文引用（/ 选择的论文/实验/想法…） */
  context?: ContextRef[];
  /** user：本条消息分享给了谁（@ 选择的目标） */
  sharedTo?: MentionTarget | null;
  /** assistant：流已结束 */
  done?: boolean;
  /** assistant：出错中断 */
  failed?: boolean;
}

/** / 上下文选择器可放入的实体类型 */
export type ContextKind = 'paper' | 'idea' | 'experiment' | 'concept';

export interface ContextRef {
  kind: ContextKind;
  id: string;
  label: string;
}

/** @ 分享目标：平台用户 or 第三方机器人 */
export type MentionKind = 'user' | 'dingtalk' | 'feishu';

export interface MentionTarget {
  kind: MentionKind;
  /** user: user_id；bot: 固定标识 */
  id: string;
  label: string;
  sub?: string;
}

/** 一段本地会话（localStorage 持久化） */
export interface Conversation {
  id: string;
  title: string;
  msgs: ChatMsg[];
  updatedAt: number;
}

export const CONTEXT_KIND_META: Record<ContextKind, { zh: string; en: string; icon: string }> = {
  paper: { zh: '论文', en: 'Paper', icon: 'book' },
  idea: { zh: '想法', en: 'Idea', icon: 'bulb' },
  experiment: { zh: '实验', en: 'Experiment', icon: 'flask' },
  concept: { zh: '概念', en: 'Concept', icon: 'layers' },
};
