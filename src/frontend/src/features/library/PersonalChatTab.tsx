import { useCallback, useMemo } from 'react';
import { useNavigate } from 'react-router-dom';
import { Markdown } from '../../lib/markdown';
import { chatPersonalSse } from '../../lib/sse';
import { tr } from '../../lib/i18n';
import { ChatSurface } from '../chat/ChatSurface';
import { ChatFigure, SourceList, makeCitationRenderer } from '../chat/LiteratureChatSources';
import type { ChatMsg } from '../chat/types';

/* ============================================================
   个人文献库对话 Tab：就「我的收藏」这批个人文献做问答，
   不绑任何课题（无 project 上下文）。壳复用 ChatSurface；
   来源清单容忍 status/relevance 为 null。
   ============================================================ */

const SUGGESTIONS: { zh: string; en: string }[] = [
  {
    zh: '我收藏的这些文献大致围绕哪几个主题？',
    en: 'What themes do my saved papers roughly cluster into?',
  },
  {
    zh: '在我收藏的文献里，帮我找出研究同一个问题的几篇。',
    en: 'Among my saved papers, find the ones tackling the same problem.',
  },
  {
    zh: '综合我收藏的文献，最近有哪些值得关注的新做法？',
    en: 'Across my saved papers, what recent approaches are worth noting?',
  },
];

export function PersonalChatTab() {
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
      chatPersonalSse({ question: args.question, history: args.history }, {
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
      surfaceKey="personal"
      pid=""
      title={tr('个人文献库对话', 'My library chat')}
      contextKinds={['paper']}
      hint={tr(
        '只就我收藏的这批个人文献回答，不限某个课题；[n] 为引用来源编号。',
        'Answers stay within your saved personal papers, across all topics. [n] marks a source number.',
      )}
      emptyIcon="chat"
      emptyTitle={tr('和我收藏的文献对话', 'Chat with my saved papers')}
      emptyDesc={tr(
        '跨课题地问我收藏的文献：找主题、比做法、理线索都行；/ 放入指定论文。',
        'Ask across the papers you saved — find themes, compare methods, trace threads. Use / to pin papers.',
      )}
      suggestions={SUGGESTIONS}
      placeholder={tr('就我收藏的文献提问，或输入 / 放入上下文…', 'Ask about your saved papers, or type / for context…')}
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
