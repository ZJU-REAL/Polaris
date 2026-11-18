import { tr } from '../../lib/i18n';
import type { WikiLinkHandler } from '../../lib/markdown';
import { LibraryChatTab } from './LibraryChatTab';
import { LibraryQaTab } from './LibraryQaTab';
import type { AskMode } from './libraryTabs';

/* ============================================================
   文献库的「问答」页签：原来的「对话」和「深度问答」合成一个入口。

     快速对话  多轮、边答边出字，按文献库全文找依据
     深入回答  一问一答，先把全库翻一遍、按引用关系补找，回答里每句都标出处；要等几十秒

   助手（⌘J）目前只能按课题收窄范围，不能只看一个文献库，所以问答还留在这一页。
   ============================================================ */


export function AskTab({
  pid,
  libraryId,
  mode,
  onMode,
  onOpenPaper,
  onWikiLink,
}: {
  pid?: string;
  libraryId: string;
  mode: AskMode;
  onMode: (m: AskMode) => void;
  onOpenPaper: (id: string) => void;
  onWikiLink: WikiLinkHandler;
}) {
  const modes: { v: AskMode; label: string; hint: string }[] = [
    {
      v: 'quick',
      label: tr('快速对话', 'Quick chat'),
      hint: tr('可以接着追问，回答边写边出。', 'Ask follow-ups; the answer streams in.'),
    },
    {
      v: 'thorough',
      label: tr('深入回答', 'Thorough answer'),
      hint: tr('翻遍整个文献库再回答，每句标出处，要等几十秒。', 'Reads the whole library first and cites every claim. Takes a little while.'),
    },
  ];
  const current = modes.find((m) => m.v === mode) ?? modes[0]!;
  return (
    <div className="kn-wrap">
      <div className="kn-nav" role="radiogroup" aria-label={tr('回答方式', 'How to answer')}>
        {modes.map((m) => (
          <button
            key={m.v}
            type="button"
            role="radio"
            aria-checked={mode === m.v}
            className={'pl-qtab' + (mode === m.v ? ' is-on' : '')}
            data-testid={`ask-${m.v}`}
            onClick={() => onMode(m.v)}
          >
            {m.label}
          </button>
        ))}
        <span className="kn-hint">{current.hint}</span>
      </div>
      <div className="kn-body">
        {mode === 'thorough' ? (
          <LibraryQaTab libraryId={libraryId} onOpenPaper={onOpenPaper} />
        ) : (
          <LibraryChatTab pid={pid} libraryId={libraryId} onOpenPaper={onOpenPaper} onWikiLink={onWikiLink} />
        )}
      </div>
    </div>
  );
}
