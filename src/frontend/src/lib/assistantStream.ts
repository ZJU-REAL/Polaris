import { postSse } from './sse';

/* ============================================================
   助手的事件流 → 可渲染的块时间线。

   **防御式**是这里的第一原则：SSE 过来的是 JSON.parse 出来的 any，TypeScript 只在
   编译期帮忙。未知事件忽略、字段缺失兜底，绝不抛——这个项目没有前端测试工具，
   运行时炸了没人拦得住。
   ============================================================ */

export interface PlanStep {
  title: string;
  status: 'pending' | 'running' | 'done';
}

export type AssistantBlock =
  | { kind: 'text'; text: string }
  | { kind: 'plan'; steps: PlanStep[] }
  | { kind: 'thinking'; text: string }
  | {
      kind: 'tool';
      id: string;
      name: string;
      state: 'running' | 'ok' | 'error';
      summary?: string;
      preview?: string;
      durationMs?: number;
    };

export interface AssistantHandlers {
  /** 用户此刻在看的页面（PolarisBuddy 的页面感知）；不传就不带 */
  page?: { kind: string; id?: string };
  /** 用「上一份块列表 → 新块列表」的形式增量更新（React 状态直接套用）。 */
  onBlocks: (fn: (blocks: AssistantBlock[]) => AssistantBlock[]) => void;
  onDone: (stopReason: string) => void;
  onError: (detail: string) => void;
}

function parse(data: string): Record<string, unknown> {
  try {
    const parsed: unknown = JSON.parse(data);
    return parsed && typeof parsed === 'object' ? (parsed as Record<string, unknown>) : {};
  } catch {
    return {};
  }
}

const str = (v: unknown, fallback = ''): string => (typeof v === 'string' ? v : fallback);
const num = (v: unknown): number | undefined => (typeof v === 'number' ? v : undefined);

/** 跑一轮助手对话；返回中止函数。
 *
 * 不带课题作用域：PolarisBuddy 是全局助手，检索范围是「这个人看得见的全部文献库」，
 * 由后端按可见性算。以前这里要传 projectId，多课题不传就 409——那是在让用户替一个
 * 纯内部的数据结构做选择题。
 */
export function assistantTurnSse(
  conversationId: string,
  question: string,
  handlers: AssistantHandlers,
): () => void {
  return postSse(
    `/chat/conversations/${conversationId}/turn`,
    {
      question,
      ...(handlers.page ? { page_kind: handlers.page.kind, page_id: handlers.page.id } : {}),
    },
    {
      onEvent: (event, raw) => {
        const data = parse(raw);
        if (event === 'delta') {
          const text = str(data.text);
          if (!text) return;
          // 追加到尾部文本块；尾块不是 text 就新开一个
          handlers.onBlocks((blocks) => {
            const last = blocks[blocks.length - 1];
            if (last && last.kind === 'text') {
              return [...blocks.slice(0, -1), { kind: 'text', text: last.text + text }];
            }
            return [...blocks, { kind: 'text', text }];
          });
        } else if (event === 'thinking') {
          const text = str(data.text);
          if (!text) return;
          handlers.onBlocks((blocks) => {
            const last = blocks[blocks.length - 1];
            if (last && last.kind === 'thinking') {
              return [...blocks.slice(0, -1), { kind: 'thinking', text: last.text + text }];
            }
            return [...blocks, { kind: 'thinking', text }];
          });
        } else if (event === 'plan') {
          // 计划是**替换**不是追加：后端每次发全量，界面上永远只有一份最新的。
          // 就地更新（而不是插到末尾）才不会让进度条在对话里越滚越多。
          const steps = Array.isArray(data.steps)
            ? (data.steps as unknown[]).flatMap((raw) => {
                if (!raw || typeof raw !== 'object') return [];
                const step = raw as Record<string, unknown>;
                const title = str(step.title);
                if (!title) return [];
                const status = str(step.status, 'pending');
                return [
                  {
                    title,
                    status: (['pending', 'running', 'done'] as const).includes(
                      status as PlanStep['status'],
                    )
                      ? (status as PlanStep['status'])
                      : ('pending' as const),
                  },
                ];
              })
            : [];
          if (!steps.length) return;
          handlers.onBlocks((blocks) => {
            const at = blocks.findIndex((b) => b.kind === 'plan');
            const block: AssistantBlock = { kind: 'plan', steps };
            if (at < 0) return [...blocks, block];
            return blocks.map((b, i) => (i === at ? block : b));
          });
        } else if (event === 'tool_call') {
          const id = str(data.id);
          handlers.onBlocks((blocks) => [
            ...blocks,
            { kind: 'tool', id, name: str(data.name, '?'), state: 'running' },
          ]);
        } else if (event === 'tool_result') {
          const id = str(data.id);
          handlers.onBlocks((blocks) =>
            blocks.map((b) =>
              b.kind === 'tool' && b.id === id
                ? {
                    ...b,
                    state: data.ok === false ? 'error' : 'ok',
                    summary: str(data.summary) || undefined,
                    preview: str(data.preview) || undefined,
                    durationMs: num(data.duration_ms),
                  }
                : b,
            ),
          );
        } else if (event === 'done') {
          handlers.onDone(str(data.stop_reason, 'stop'));
        } else if (event === 'error') {
          handlers.onError(str(data.detail, '出错了'));
        }
        // 其余事件（meta / usage / compaction / 将来新增的）一律忽略：
        // 后端加事件不该让旧前端崩掉
      },
      onClose: () => handlers.onDone('stop'),
      onError: (err) => handlers.onError(err instanceof Error ? err.message : String(err)),
    },
  );
}
