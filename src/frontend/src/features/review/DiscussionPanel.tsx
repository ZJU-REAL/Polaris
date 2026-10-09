import { useEffect, useRef, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Icon } from '../../components/ui/Icon';
import { toast } from '../../components/ui/Toast';
import { api, type ReviewMessageRead } from '../../lib/api';
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
    onError: (e) => toast(`${tr('发送失败：', 'Couldn’t send: ')}${e instanceof Error ? e.message : String(e)}`, 'error'),
  });

  function send() {
    const content = draft.trim();
    if (!content || !sid || sendMutation.isPending) return;
    sendMutation.mutate(content);
  }

  return (
    <div className="card" style={{ overflow: 'hidden' }}>
      <div className="card-pad row" style={{ paddingBottom: 12, justifyContent: 'space-between' }}>
        <span className="section-h">
          {tr('讨论区', 'Discussion')}
        </span>
        {messages.length > 0 && (
          <span className="mono" style={{ fontSize: 11, color: 'var(--text-3)' }}>
            {tr(`${messages.length} 条`, `${messages.length} ${messages.length === 1 ? 'message' : 'messages'}`)}
          </span>
        )}
      </div>

      {/* 提示 */}
      <div style={{ margin: '-6px 22px 12px', fontSize: 12, color: 'var(--text-3)', lineHeight: 1.5 }}>
        {tr('下次评审时，AI 审稿人会参考这里的评论。', 'AI reviewers read these comments in the next review.')}
      </div>

      {/* 消息列表 */}
      <div ref={listRef} className="scroll" style={{ maxHeight: 380, overflowY: 'auto', padding: '4px 22px 8px' }}>
        {sessionsQuery.isLoading || (sid && messagesQuery.isLoading) ? (
          <div className="empty" style={{ padding: 24 }}>{tr('加载中…', 'Loading…')}</div>
        ) : sessionsQuery.isError ? (
          <div className="empty" style={{ padding: 24 }}>
            {tr('无法加载讨论，请确认本机引擎正在运行', 'Couldn’t load the discussion. Make sure the local engine is running.')}
          </div>
        ) : !session ? (
          <div className="empty" style={{ padding: 24 }}>
            {tr('讨论暂不可用', 'Discussion isn’t available yet')}
          </div>
        ) : messages.length === 0 ? (
          <div className="empty" style={{ padding: 24 }}>
            {tr('还没有评论', 'No comments yet')}
          </div>
        ) : (
          messages.map((m) => <DiscussionBubble key={m.id} msg={m} />)
        )}
      </div>

      {/* 输入框 */}
      <div className="row gap10" style={{ padding: '12px 22px 18px', borderTop: '0.5px solid var(--border)' }}>
        <textarea
          className="textarea"
          rows={2}
          placeholder={
            session
              ? tr('写评论，Enter 发送，Shift Enter 换行', 'Write a comment. Enter to send, Shift Enter for a new line')
              : tr('讨论暂不可用', 'Discussion unavailable')
          }
          value={draft}
          disabled={!session || sendMutation.isPending}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
              e.preventDefault();
              send();
            }
          }}
          style={{ flex: 1, minHeight: 44 }}
        />
        <button
          className="btn btn-primary"
          disabled={!session || !draft.trim() || sendMutation.isPending}
          onClick={send}
          style={{ alignSelf: 'flex-end' }}
        >
          {sendMutation.isPending ? (
            <Icon name="refresh" size={14} style={{ animation: 'spin 1s linear infinite' }} />
          ) : (
            <Icon name="arrow" size={14} />
          )}
          {tr('发送', 'Send')}
        </button>
      </div>
    </div>
  );
}
