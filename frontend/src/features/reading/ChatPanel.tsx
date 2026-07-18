import { useCallback } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { Icon } from '../../components/ui/Icon';
import { toast } from '../../components/ui/Toast';
import { Markdown } from '../../lib/markdown';
import { api } from '../../lib/api';
import { chatPaperSse } from '../../lib/sse';
import { tr } from '../../lib/i18n';
import { ChatSurface } from '../chat/ChatSurface';
import type { ChatMsg } from '../chat/types';

/* ============================================================
   阅读工作台 · AI 伴读面板：复用 ChatSurface 的壳
   （历史 / 技能条 / / 上下文 / @ 分享），
   伴读特有：每条回答可「存为笔记」，分享附带本篇阅读链接。
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
    onError: (e) => toast(`${tr('保存失败：', 'Save failed: ')}${e instanceof Error ? e.message : String(e)}`, 'error'),
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

export function ChatPanel({ paperId, pid }: { paperId: string; pid: string }) {
  const stream = useCallback(
    (
      args: { question: string; history: { role: 'user' | 'assistant'; content: string }[] },
      ctrl: { onDelta: (t: string) => void; onDone: () => void; onError: (d: string) => void },
    ) =>
      chatPaperSse(paperId, { question: args.question, history: args.history }, {
        onEvent: (event, dataStr) => {
          if (event === 'delta') {
            try {
              const t = (JSON.parse(dataStr) as { text?: string }).text ?? '';
              if (t) ctrl.onDelta(t);
            } catch {
              /* ignore */
            }
          } else if (event === 'done') ctrl.onDone();
          else if (event === 'error') {
            let detail = tr('服务端出错', 'Server error');
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
      }),
    [paperId],
  );

  const readLink = `${window.location.origin}/papers/${paperId}/read`;

  return (
    <ChatSurface
      surfaceKey={`reading:${paperId}`}
      pid={pid}
      title={tr('AI 伴读', 'AI reading')}
      contextKinds={['paper', 'concept']}
      defaultDrawerOpen={false}
      attachesPaperLink
      shareLink={readLink}
      hint={tr(
        '回答基于论文全文，仅供参考——关键结论请回到原文核对。',
        'Answers are based on the full paper text and for reference only — verify against the original.',
      )}
      emptyIcon="chat"
      emptyTitle={tr('问问 AI 这篇论文', 'Ask AI about this paper')}
      emptyDesc={tr(
        '比如它解决了什么问题、方法核心、与前作差别；@ 可把推荐连同阅读链接分享给同事。',
        'e.g. the problem, the core method, the difference from prior work; use @ to share with the read link.',
      )}
      placeholder={tr('针对这篇论文提问，或 @ 分享…', 'Ask about this paper, or @ to share…')}
      renderAssistant={(m: ChatMsg) => <Markdown source={m.content} style={{ fontSize: 12.5 }} />}
      messageActions={(m: ChatMsg) => <SaveNoteButton content={m.content} paperId={paperId} pid={pid} />}
      stream={stream}
    />
  );
}
