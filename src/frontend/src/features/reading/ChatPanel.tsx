import { useCallback } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { Icon } from '../../components/ui/Icon';
import { toast } from '../../components/ui/Toast';
import { Markdown } from '../../lib/markdown';
import { api, type LibraryChatSource } from '../../lib/api';
import { chatPaperSse } from '../../lib/sse';
import { tr } from '../../lib/i18n';
import { ChatSurface } from '../chat/ChatSurface';
import type { ChatMsg, ContextRef } from '../chat/types';

/* ============================================================
   阅读工作台 · AI 伴读面板：复用 ChatSurface 的壳
   （历史 / 技能条 / / 上下文 / @ 分享），
   伴读特有：每条回答可存为笔记，分享附带本篇阅读链接。
   ============================================================ */

function SaveNoteButton({ content, paperId, pid }: { content: string; paperId: string; pid: string }) {
  const queryClient = useQueryClient();
  const saveNote = useMutation({
    mutationFn: (c: string) => api.createPaperNote(paperId, c),
    onSuccess: () => {
      toast(tr('已存为笔记', 'Saved as note'), 'ok');
      void queryClient.invalidateQueries({ queryKey: ['paper-notes', paperId] });
      void queryClient.invalidateQueries({ queryKey: ['project-notes', pid] });
      void queryClient.invalidateQueries({ queryKey: ['paper', paperId] });
      void queryClient.invalidateQueries({ queryKey: ['papers', pid] });
    },
    onError: (e) => toast(`${tr('保存失败：', 'Couldn’t save: ')}${e instanceof Error ? e.message : String(e)}`, 'error'),
  });
  return (
    <div className="row" style={{ marginTop: 8, justifyContent: 'flex-end' }}>
      <button
        className="btn btn-soft sm"
        style={{ height: 24, fontSize: 11 }}
        disabled={saveNote.isPending}
        onClick={() => saveNote.mutate(content)}
      >
        <Icon name="pen" size={11} />
        {tr('存为笔记', 'Save as note')}
      </button>
    </div>
  );
}

/** 本次回答参考的「其他文献」清单（点击跳到那篇的阅读页）。 */
function RefList({ sources, onOpen }: { sources: LibraryChatSource[]; onOpen: (id: string) => void }) {
  if (sources.length === 0) return null;
  return (
    <div className="col" style={{ gap: 3, marginTop: 10, paddingTop: 8, borderTop: '0.5px solid var(--border)' }}>
      <span className="mono" style={{ fontSize: 10, color: 'var(--text-3)' }}>
        {tr(`参考文献 · ${sources.length} 篇`, `Sources · ${sources.length}`)}
      </span>
      {sources.map((s) => (
        <button
          key={s.index}
          onClick={() => onOpen(s.paper_id)}
          title={s.title}
          style={{
            border: 'none',
            background: 'transparent',
            cursor: 'pointer',
            padding: 0,
            fontSize: 12,
            color: 'var(--text-2)',
            textAlign: 'left',
            display: 'flex',
            gap: 6,
          }}
        >
          <span className="mono" style={{ color: 'var(--accent-text)', flexShrink: 0 }}>[{s.index}]</span>
          <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
            {s.title}
            {s.year ? `（${s.year}）` : ''}
          </span>
        </button>
      ))}
    </div>
  );
}

export function ChatPanel({ paperId, pid }: { paperId: string; pid: string }) {
  const navigate = useNavigate();
  const location = useLocation();
  const stream = useCallback(
    (
      args: {
        question: string;
        history: { role: 'user' | 'assistant'; content: string }[];
        context: ContextRef[];
      },
      ctrl: {
        onDelta: (t: string) => void;
        onSources?: (s: string) => void;
        onDone: () => void;
        onError: (d: string) => void;
      },
    ) => {
      // 只把 / 选择器里挑中的「其他论文」透传给后端（排除当前这篇自身，避免重复）
      const contextPaperIds = args.context
        .filter((c) => c.kind === 'paper' && c.id !== paperId)
        .map((c) => c.id);
      return chatPaperSse(
        paperId,
        { question: args.question, history: args.history, context_paper_ids: contextPaperIds },
        {
          onEvent: (event, dataStr) => {
            if (event === 'sources') ctrl.onSources?.(dataStr);
            else if (event === 'delta') {
              try {
                const t = (JSON.parse(dataStr) as { text?: string }).text ?? '';
                if (t) ctrl.onDelta(t);
              } catch {
                /* ignore */
              }
            } else if (event === 'done') ctrl.onDone();
            else if (event === 'error') {
              let detail = tr('本机引擎出错', 'The local engine hit an error');
              try {
                detail = (JSON.parse(dataStr) as { detail?: string }).detail ?? detail;
              } catch {
                /* keep */
              }
              ctrl.onError(detail);
            }
          },
          onClose: () => ctrl.onDone(),
          onError: (err) => ctrl.onError(err instanceof Error ? err.message : String(err)),
        },
      );
    },
    [paperId],
  );

  return (
    <ChatSurface
      surfaceKey={`reading:${paperId}`}
      pid={pid}
      title={tr('AI 问答', 'Ask AI')}
      contextKinds={['paper', 'concept']}
      defaultDrawerOpen={false}
      hint={tr(
        '基于本篇全文回答，输入 / 可加入其他论文对比。重要结论请核对原文。',
        'Answers draw on this paper. Type / to compare with others. Check key claims against the paper.',
      )}
      emptyIcon="chat"
      emptyTitle={tr('关于这篇论文提问', 'Ask about this paper')}
      emptyDesc={tr(
        '例如它解决了什么问题、核心方法是什么、和已有工作有何不同。',
        'For example: what problem it solves, its core method, how it differs from prior work.',
      )}
      placeholder={tr('提问，或输入 @ 分享', 'Ask a question, or type @ to share')}
      renderAssistant={(m: ChatMsg) => <Markdown source={m.content} style={{ fontSize: 13 }} />}
      assistantExtras={(m: ChatMsg) =>
        (m.sources?.length ?? 0) > 0 && (m.done || m.content) ? (
          <RefList sources={m.sources ?? []} onOpen={(id) => navigate(`/papers/${id}/read`, { state: location.state })} />
        ) : null
      }
      messageActions={(m: ChatMsg) => <SaveNoteButton content={m.content} paperId={paperId} pid={pid} />}
      stream={stream}
    />
  );
}
