import { postSse } from './sse';

/* ============================================================
   助手的事件流 → 可渲染的块时间线。

   **防御式**是这里的第一原则：SSE 过来的是 JSON.parse 出来的 any，TypeScript 只在
   编译期帮忙。未知事件忽略、字段缺失兜底，绝不抛——这个项目没有前端测试工具，
   运行时炸了没人拦得住。
   ============================================================ */

export type AssistantBlock =
  | { kind: 'text'; text: string }
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
 * ``projectId`` 是这轮检索工具的作用域。后端只在会话上还没存过课题时需要它，
 * 存过一次就自己认得；但每轮都带上是无害的，也省得前端追踪「存没存过」。
 * 不传且用户名下有多个课题时，后端返回 409 PROJECT_REQUIRED，由调用方弹选择。
 */
export function assistantTurnSse(
  conversationId: string,
  question: string,
  handlers: AssistantHandlers,
  projectId?: string | null,
): () => void {
  return postSse(
    `/chat/conversations/${conversationId}/turn`,
    {
      question,
      ...(projectId ? { project_id: projectId } : {}),
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
