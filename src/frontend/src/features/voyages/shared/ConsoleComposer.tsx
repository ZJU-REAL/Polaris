import { useEffect, useState, type MutableRefObject } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { Icon } from '../../../components/ui/Icon';
import { toast } from '../../../components/ui/Toast';
import { tr, useLang } from '../../../lib/i18n';
import { api, type VoyageMessageRead } from '../../../lib/api';
import { askOptionLabel } from './AskBlock';

/* ============================================================
   对话输入框（终端面板底部）：
   - 平时：发非阻塞建议（AI 在下一个决策点参考）；
   - AI 提问等回答时：黄框高亮 + 候选项 chips，发送即回答并恢复任务；
   - 「放弃」类选项要二次确认（那是唯一由用户决定的失败路径）。
   ============================================================ */

function upsertMessage(list: VoyageMessageRead[] | undefined, message: VoyageMessageRead) {
  const prev = list ?? [];
  if (prev.some((m) => m.id === message.id)) return prev;
  return [...prev, message].sort((a, b) => a.seq - b.seq);
}

export function ConsoleComposer({
  voyageId,
  openAsk,
  disabled,
  inputRef,
}: {
  voyageId: string;
  openAsk: VoyageMessageRead | null;
  /** 终态任务：没有下一个决策点，禁用输入 */
  disabled?: boolean;
  inputRef?: MutableRefObject<HTMLTextAreaElement | null>;
}) {
  const queryClient = useQueryClient();
  const lang = useLang();
  const [text, setText] = useState('');
  const [choice, setChoice] = useState<string | null>(null);

  // 切换到新提问时清掉上一个问题的选择
  useEffect(() => {
    setChoice(null);
  }, [openAsk?.id]);

  const afterSend = (message: VoyageMessageRead) => {
    queryClient.setQueryData<VoyageMessageRead[]>(['voyage-messages', voyageId], (old) =>
      upsertMessage(old, message),
    );
    void queryClient.invalidateQueries({ queryKey: ['voyage', voyageId] });
    void queryClient.invalidateQueries({ queryKey: ['experiment'] });
  };

  const suggestMutation = useMutation({
    mutationFn: (body: string) => api.postVoyageMessage(voyageId, body),
    onSuccess: (message) => {
      afterSend(message);
      setText('');
      toast(tr('已发送，AI 会在下一步决策时参考', 'Sent — the AI will factor it in at its next decision'), 'ok');
    },
    onError: (e) =>
      toast(`${tr('发送失败：', 'Send failed: ')}${e instanceof Error ? e.message : String(e)}`, 'error'),
  });

  const answerMutation = useMutation({
    mutationFn: (body: { text?: string; choice?: string }) =>
      api.answerVoyageAsk(voyageId, openAsk!.id, body),
    onSuccess: (message, _body) => {
      // 本地立刻把提问置为已回答（SSE 的 ask.answered 只带回答消息，
      // 不带更新后的提问行；不改缓存的话界面会一直卡在「等你回复」）
      const askId = message.reply_to;
      if (askId) {
        queryClient.setQueryData<VoyageMessageRead[]>(['voyage-messages', voyageId], (old) =>
          (old ?? []).map((m) => (m.id === askId ? { ...m, status: 'answered' } : m)),
        );
      }
      afterSend(message);
      setText('');
      setChoice(null);
      toast(tr('已回复，任务继续', 'Answered — the task resumes'), 'ok');
    },
    onError: (e) => {
      const notOpen = e instanceof Error && e.message.includes('ASK_NOT_OPEN');
      if (notOpen) {
        // 提问已被回答/作废（可能在别的窗口答过）：以服务端为准刷新，自愈过期界面
        void queryClient.invalidateQueries({ queryKey: ['voyage-messages', voyageId] });
        void queryClient.invalidateQueries({ queryKey: ['voyage', voyageId] });
        toast(tr('这个问题已经回复过了，界面已刷新', 'Already answered — refreshed the view'), 'info');
        return;
      }
      toast(`${tr('回复失败：', 'Reply failed: ')}${e instanceof Error ? e.message : String(e)}`, 'error');
    },
  });

  const busy = suggestMutation.isPending || answerMutation.isPending;
  const options = openAsk?.payload?.options ?? [];

  const send = () => {
    const body = text.trim();
    if (openAsk) {
      if (!body && !choice) return;
      if (choice === 'abort' && !window.confirm(tr('确定放弃这个任务？放弃后无法恢复。', 'Give up this task? This cannot be undone.'))) {
        return;
      }
      answerMutation.mutate({ text: body || undefined, choice: choice ?? undefined });
    } else {
      if (!body) return;
      suggestMutation.mutate(body);
    }
  };

  return (
    <div
      style={{
        marginTop: 10,
        padding: 10,
        borderRadius: 12,
        border: openAsk ? '1.5px solid var(--warn-tx)' : '0.5px solid var(--border)',
        background: 'var(--surface)',
      }}
    >
      {openAsk && (
        <div className="col gap8" style={{ marginBottom: 8 }}>
          <div className="row gap6" style={{ fontSize: 12, color: 'var(--warn-tx)', fontWeight: 650 }}>
            <span className="dot pulse" style={{ background: 'var(--warn-tx)' }} />
            {tr('AI 正在等你回复：', 'The AI is waiting for your reply: ')}
            <span style={{ color: 'var(--text)', fontWeight: 600, minWidth: 0 }}>{openAsk.text}</span>
          </div>
          {options.length > 0 && (
            <div className="row gap6 wrap">
              {options.map((opt) => {
                const active = choice === opt.id;
                const danger = opt.id === 'abort';
                return (
                  <button
                    key={opt.id}
                    onClick={() => setChoice(active ? null : opt.id)}
                    className="pill sm"
                    style={{
                      cursor: 'pointer',
                      border: active ? '1.5px solid currentColor' : '0.5px solid var(--border)',
                      background: active
                        ? danger
                          ? 'var(--danger-bg)'
                          : 'var(--accent-soft)'
                        : 'var(--surface-2)',
                      color: active
                        ? danger
                          ? 'var(--danger-tx)'
                          : 'var(--accent-text)'
                        : 'var(--text-2)',
                      fontWeight: active ? 700 : 500,
                    }}
                  >
                    {askOptionLabel(opt, lang)}
                  </button>
                );
              })}
            </div>
          )}
        </div>
      )}
      <div className="row gap8" style={{ alignItems: 'flex-end' }}>
        <textarea
          ref={inputRef}
          className="textarea"
          rows={2}
          value={text}
          onChange={(e) => setText(e.target.value)}
          disabled={disabled || busy}
          placeholder={
            disabled
              ? tr('任务已结束', 'Task finished')
              : openAsk
                ? tr('输入你的回复（可只选上方选项直接发送）…', 'Type your reply (or just pick an option above)…')
                : tr('随时给 AI 建议，会在下一步决策时参考…', 'Suggest anything — the AI factors it in at its next decision…')
          }
          style={{ flex: 1, resize: 'vertical', minHeight: 44, fontSize: 12.5 }}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
              e.preventDefault();
              send();
            }
          }}
        />
        <button
          className="btn btn-primary"
          disabled={disabled || busy || (openAsk ? !text.trim() && !choice : !text.trim())}
          onClick={send}
          title={tr('发送（⌘/Ctrl+Enter）', 'Send (⌘/Ctrl+Enter)')}
        >
          <Icon name="arrow" size={13} />
          {busy ? tr('发送中…', 'Sending…') : openAsk ? tr('回复', 'Reply') : tr('发送', 'Send')}
        </button>
      </div>
    </div>
  );
}
