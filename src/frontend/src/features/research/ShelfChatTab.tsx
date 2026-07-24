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
   相关研究对话 Tab：只就本课题「相关研究」里的这批论文做问答，
   范围比整个文献库小、更贴题。壳复用 ChatSurface；来源清单
   容忍 status/relevance 为 null（scoped 场景后端可能不给）。
   ============================================================ */

const SUGGESTIONS: { zh: string; en: string }[] = [
  {
    zh: '这些相关研究里，哪几篇和我这个课题最直接相关？为什么？',
    en: 'Which of these related papers matter most to my topic, and why?',
  },
  {
    zh: '把这批相关研究的做法归归类，各自的思路差在哪？',
    en: 'Group the approaches in this related work — how do their ideas differ?',
  },
  {
    zh: '综合这些相关研究，还有哪些没被解决、值得我去做的问题？',
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
    [pid],
  );

  return (
    <ChatSurface
      surfaceKey={`shelf:${pid}`}
      pid={pid}
      title={tr('相关研究对话', 'Related work chat')}
      contextKinds={['paper', 'idea', 'experiment', 'concept']}
      hint={tr(
        '只就本课题「相关研究」里的这批论文回答，问对比、归类、找空白都行；[n] 为引用来源编号。',
        'Answers stay within this topic’s related work; ask for comparisons, groupings or open problems. [n] marks a source number.',
      )}
      headerAction={<BuildIndexButton build={() => api.buildShelfIndex(pid)} />}
      emptyIcon="chat"
      emptyTitle={tr('和本课题的相关研究对话', 'Chat with this topic’s related work')}
      emptyDesc={tr(
        '范围锁定在你加进「相关研究」的这批论文，比通用文献库更贴题；/ 放入指定论文，@ 分享给同事。',
        'Scoped to the papers you shelved as related work — closer to your topic than the whole library. Use / to pin papers, @ to share.',
      )}
      suggestions={SUGGESTIONS}
      placeholder={tr('就本课题相关研究提问，或输入 / 放入上下文、@ 分享…', 'Ask about this related work, or type / for context, @ to share…')}
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
