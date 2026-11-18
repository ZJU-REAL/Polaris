import { useState } from 'react';
import { useMutation } from '@tanstack/react-query';
import { api, type VoyageMessageRead } from '../../lib/api';
import { errorText } from '../../lib/errors';
import { tr, useLang } from '../../lib/i18n';
import { askOptionLabel } from '../voyages/shared/AskBlock';

/* ============================================================
   研究过程中 AI 提的问题，直接在时间线上回答（浅色版；任务详情页那个是终端配色）。
   有候选项就点一个；也可以自己写一句。回答后任务继续。
   ============================================================ */

export interface InlineQuestionProps {
  voyageId: string;
  ask: VoyageMessageRead;
  onAnswered: () => void;
}

export function InlineQuestion({ voyageId, ask, onAnswered }: InlineQuestionProps) {
  const lang = useLang();
  const [text, setText] = useState('');
  const options = (ask.payload?.options ?? []).filter((o) => o.id !== 'abort');

  const answer = useMutation({
    mutationFn: (input: { choice?: string; text?: string }) => api.answerVoyageAsk(voyageId, ask.id, input),
    onSuccess: onAnswered,
    // 已经回答过（另一个窗口、或刚才那次其实成功了）：刷新就好，不算错
    onError: (e) => {
      if (e instanceof Error && e.message.startsWith('ASK_NOT_OPEN')) onAnswered();
    },
  });
  const busy = answer.isPending;
  const failed = answer.isError && !(answer.error instanceof Error && answer.error.message.startsWith('ASK_NOT_OPEN'));

  function sendText() {
    const t = text.trim();
    if (!t || busy) return;
    answer.mutate({ text: t });
  }

  return (
    <div className="dr-ask" role="group" aria-label={tr('需要你回答的问题', 'A question for you')}>
      <div className="dr-ask-text">{ask.text}</div>
      {options.length > 0 && (
        <div className="dr-ask-options">
          {options.map((opt) => (
            <button
              key={opt.id}
              type="button"
              className="btn btn-ghost"
              disabled={busy}
              onClick={() => answer.mutate({ choice: opt.id })}
            >
              {askOptionLabel(opt, lang)}
            </button>
          ))}
        </div>
      )}
      <div className="dr-ask-reply">
        <input
          className="input"
          value={text}
          disabled={busy}
          aria-label={tr('你的回答', 'Your answer')}
          placeholder={options.length > 0 ? tr('或者写下你的想法', 'Or write your own answer') : tr('写下你的回答', 'Write your answer')}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !e.nativeEvent.isComposing && e.keyCode !== 229) {
              e.preventDefault();
              sendText();
            }
          }}
        />
        <button type="button" className="btn btn-ghost" disabled={busy || text.trim() === ''} onClick={sendText}>
          {busy ? tr('正在发送…', 'Sending…') : tr('发送', 'Send')}
        </button>
      </div>
      {failed && (
        <div className="dr-ask-error" role="alert">
          {errorText(answer.error)}
        </div>
      )}
    </div>
  );
}
