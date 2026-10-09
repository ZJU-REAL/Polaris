import { useCallback, useMemo } from 'react';
import { useNavigate } from 'react-router-dom';
import { Markdown } from '../../lib/markdown';
import { chatShelfSse } from '../../lib/sse';
import { tr } from '../../lib/i18n';
import { api } from '../../lib/api';
import { ChatSurface } from '../chat/ChatSurface';
import { ChatFigure, SourceList, makeCitationRenderer } from '../chat/LiteratureChatSources';
import { BuildIndexButton } from '../chat/BuildIndexButton';
import type { ChatMsg } from '../chat/types';

/* ============================================================
   相关研究对话 Tab：只就本课题相关研究里的这批论文做问答，
   范围比整个文献库小、更贴题。壳复用 ChatSurface；来源清单
   容忍 status/relevance 为 null（scoped 场景后端可能不给）。
   ============================================================ */

const SUGGESTIONS: { zh: string; en: string }[] = [
  {
    zh: '哪几篇和我的课题最直接相关？为什么？',
    en: 'Which of these related papers matter most to my topic, and why?',
  },
  {
    zh: '把这些论文的方法归类，它们的思路有什么不同？',
    en: 'Group these papers by approach. How do their ideas differ?',
  },
  {
    zh: '还有哪些没解决、值得我做的问题？',
    en: 'Across this related work, which open problems are worth pursuing?',
  },
];

export interface ShelfChatTabProps {
  pid: string;
}

export function ShelfChatTab({ pid }: ShelfChatTabProps) {
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
      chatShelfSse(pid, { question: args.question, history: args.history }, {
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
      }),
    [pid],
  );

  return (
    <ChatSurface
      surfaceKey={`shelf:${pid}`}
      pid={pid}
      title={tr('相关研究对话', 'Related work chat')}
      contextKinds={['paper', 'idea', 'experiment', 'concept']}
      hint={tr(
        '只根据相关研究中的论文回答，[n] 表示来源。',
        'Answers use only your related work. [n] marks a source.',
      )}
      headerAction={<BuildIndexButton build={() => api.buildShelfIndex(pid)} />}
      emptyIcon="chat"
      emptyTitle={tr('就相关研究提问', 'Ask about your related work')}
      emptyDesc={tr(
        '回答只基于你加入相关研究的论文。',
        'Answers draw only on the papers you added to related work.',
      )}
      suggestions={SUGGESTIONS}
      placeholder={tr('提问，或输入 / 添加上下文…', 'Ask a question, or type / to add context…')}
      renderAssistant={(m: ChatMsg) => (
        <Markdown
          source={m.content}
          style={{ fontSize: 13 }}
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
