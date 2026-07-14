import { useEffect, useRef, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Icon } from '../../components/ui/Icon';
import { toast } from '../../components/ui/Toast';
import { api, type ReviewMessageRead } from '../../lib/api';
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
    onError: (e) => toast(`发送失败：${e instanceof Error ? e.message : String(e)}`, 'error'),
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
          <Icon name="users" size={15} style={{ color: 'var(--accent)' }} />
          讨论区 <span className="en-label" style={{ fontSize: 11 }}>Discussion · 人机同场</span>
        </span>
        {messages.length > 0 && (
          <span className="mono" style={{ fontSize: 11, color: 'var(--text-3)' }}>{messages.length} 条</span>
        )}
      </div>

      {/* 提示条 */}
      <div
        className="row gap8"
        style={{
          margin: '0 22px 12px',
          padding: '8px 12px',
          borderRadius: 9,
          background: 'var(--accent-soft)',
          fontSize: 11.5,
          color: 'var(--accent-text)',
          lineHeight: 1.5,
        }}
      >
        <Icon name="sparkle" size={13} style={{ flexShrink: 0 }} />
        你的评论会作为上下文进入下一轮 agent 评审 · your comments feed the next review round
      </div>

      {/* 消息列表 */}
      <div ref={listRef} className="scroll" style={{ maxHeight: 380, overflowY: 'auto', padding: '4px 22px 8px' }}>
        {sessionsQuery.isLoading || (sid && messagesQuery.isLoading) ? (
          <div className="empty" style={{ padding: 24 }}>加载讨论…</div>
        ) : sessionsQuery.isError ? (
          <div className="empty" style={{ padding: 24 }}>无法加载讨论区（后端不可用或接口未就绪）</div>
        ) : !session ? (
          <div className="empty" style={{ padding: 24 }}>讨论区尚未创建（后端未就绪）</div>
        ) : messages.length === 0 ? (
          <div className="empty" style={{ padding: 24 }}>还没有讨论 — 说点什么，给 agent 评审提供人类视角</div>
        ) : (
          messages.map((m) => <DiscussionBubble key={m.id} msg={m} />)
        )}
      </div>

      {/* 输入框 */}
      <div className="row gap10" style={{ padding: '12px 22px 18px', borderTop: '0.5px solid var(--border)' }}>
        <textarea
          className="textarea"
          rows={2}
          placeholder={session ? '写下你的评论…（Enter 发送，Shift+Enter 换行）' : '讨论区不可用'}
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
          发送
        </button>
      </div>
    </div>
  );
}
