import { useEffect, useRef, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { LoadingState } from '../../components/ui/EmptyState';
import { toast } from '../../components/ui/Toast';
import { api, type ReviewMessageRead } from '../../lib/api';
import { errorText } from '../../lib/errors';
import { tr } from '../../lib/i18n';
import { DiscussionBubble } from './messages';

/* ============================================================
   人机同场讨论区（idea 详情页底部）：
   GET /ideas/{id}/sessions 找 idea_discussion（后端惰性创建）
   → GET /sessions/{sid}/messages 渲染气泡
   → 底部输入框 POST message。
   WS review.message 由 AppShell 直接写入
   ['session-messages', sid] cache，实现实时追加。
   ============================================================ */

export function DiscussionPanel({ ideaId }: { ideaId: string }) {
  const queryClient = useQueryClient();
  const [draft, setDraft] = useState('');
  const listRef = useRef<HTMLDivElement | null>(null);

  const sessionsQuery = useQuery({
    queryKey: ['idea-sessions', ideaId],
    queryFn: () => api.listIdeaSessions(ideaId),
    retry: false,
  });
  const session = sessionsQuery.data?.find((s) => s.target_type === 'idea_discussion') ?? null;
  const sid = session?.id ?? null;

  const messagesQuery = useQuery({
    queryKey: ['session-messages', sid],
    queryFn: () => api.listSessionMessages(sid!),
    enabled: !!sid,
    retry: false,
    // WS 为主，轮询兜底
    refetchInterval: 30_000,
  });
  const messages = messagesQuery.data ?? [];

  // 新消息到达时滚到底部
  useEffect(() => {
    const el = listRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [messages.length]);

  const sendMutation = useMutation({
    mutationFn: (content: string) => api.postSessionMessage(sid!, content),
    onSuccess: (msg) => {
      setDraft('');
      queryClient.setQueryData<ReviewMessageRead[]>(['session-messages', sid], (old) =>
        old === undefined ? [msg] : old.some((m) => m.id === msg.id) ? old : [...old, msg],
      );
    },
    onError: (e) => toast(errorText(e), 'error'),
  });

  function send() {
    const content = draft.trim();
    if (!content || !sid || sendMutation.isPending) return;
    sendMutation.mutate(content);
  }

  return (
    <div className="idea-comments">
      <div ref={listRef} className="scroll" style={{ maxHeight: 380, overflowY: 'auto' }}>
        {sessionsQuery.isLoading || (sid && messagesQuery.isLoading) ? (
          <LoadingState compact rows={2} />
        ) : sessionsQuery.isError ? (
          <div className="idea-empty">{tr('无法加载讨论，请确认本机引擎正在运行', 'Couldn’t load the discussion. Make sure the local engine is running.')}</div>
        ) : !session ? (
          <div className="idea-empty">{tr('讨论暂不可用', 'Discussion isn’t available yet')}</div>
        ) : messages.length === 0 ? null : (
          messages.map((m) => <DiscussionBubble key={m.id} msg={m} />)
        )}
      </div>
      <div className="idea-comment-form">
        <textarea
          className="textarea"
          rows={1}
          aria-label={tr('写评论', 'Write a comment')}
          placeholder={session ? tr('写下你对这个想法的看法', 'Write what you think of this idea') : tr('讨论暂不可用', 'Discussion unavailable')}
          value={draft}
          disabled={!session || sendMutation.isPending}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
              e.preventDefault();
              send();
            }
          }}
        />
        <button className="btn btn-ghost" disabled={!session || !draft.trim() || sendMutation.isPending} onClick={send}>
          {sendMutation.isPending ? tr('发送中…', 'Sending…') : tr('发送', 'Send')}
        </button>
      </div>
    </div>
  );
}
