import { useCallback, useMemo } from 'react';
import { useNavigate } from 'react-router-dom';
import { Markdown } from '../../lib/markdown';
import { chatDailySse } from '../../lib/sse';
import { tr } from '../../lib/i18n';
import { ChatSurface } from '../chat/ChatSurface';
import { ChatFigure, SourceList, makeCitationRenderer } from '../chat/LiteratureChatSources';
import type { ChatMsg } from '../chat/types';

/* ============================================================
   每日新论文池对话 Tab：就最近 7 天池内的每日新论文做问答，
   不绑课题、不需要建索引。壳复用 ChatSurface，
   接法与 PersonalChatTab（个人库对话）完全同构。
   ============================================================ */

const SUGGESTIONS: { zh: string; en: string }[] = [
  {
    zh: '最近 7 天有哪些值得关注的新论文？',
    en: 'What notable new papers appeared in the last 7 days?',
  },
  {
    zh: '帮我把这几天的新论文按主题大致归归类。',
    en: 'Roughly group the recent new papers by theme.',
  },
  {
    zh: '这几天有没有和大模型智能体相关的新工作？',
    en: 'Any new work related to LLM agents in the last few days?',
  },
];

export function DailyChatTab() {
  const navigate = useNavigate();
  const openPaper = useCallback((id: string) => navigate(`/papers/${id}/read`), [navigate]);
  const citationRenderer = useMemo(() => makeCitationRenderer(openPaper), [openPaper]);

  const stream = useCallback(
    (
      args: { question: string; history: { role: 'user' | 'assistant'; content: string }[] },
      ctrl: {
        onDelta: (t: string) => void;
        onSources?: (s: string) => void;
        onDone: () => void;
        onError: (d: string) => void;
      },
    ) =>
      chatDailySse({ question: args.question, history: args.history }, {
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
    [],
  );

  return (
    <ChatSurface
      surfaceKey="daily"
      pid=""
      title={tr('每日新论文对话', 'Daily papers chat')}
      contextKinds={[]}
      hint={tr(
        '对最近 7 天的每日新论文提问；[n] 为引用来源编号。',
        'Ask about the last 7 days of daily papers. [n] marks a source number.',
      )}
      emptyIcon="chat"
      emptyTitle={tr('和最近的新论文对话', 'Chat with the latest papers')}
      emptyDesc={tr(
        '就池里最近 7 天的每日新论文提问：找热点、比方法、挑值得细读的都行。',
        'Ask about the daily papers from the last 7 days — spot trends, compare methods, pick what to read.',
      )}
      suggestions={SUGGESTIONS}
      placeholder={tr('就最近 7 天的新论文提问…', 'Ask about the last 7 days of new papers…')}
      renderAssistant={(m: ChatMsg) => (
        <Markdown
          source={m.content}
          style={{ fontSize: 12.5 }}
          renderCitation={citationRenderer(m.sources)}
          renderLibraryFigure={(paperId, index) => (
            <ChatFigure paperId={paperId} index={index} onOpenPaper={openPaper} />
          )}
        />
      )}
      assistantExtras={(m: ChatMsg) =>
        (m.sources?.length ?? 0) > 0 && (m.done || m.content) ? (
          <SourceList sources={m.sources ?? []} onOpenPaper={openPaper} />
        ) : null
      }
      stream={stream}
    />
  );
}
