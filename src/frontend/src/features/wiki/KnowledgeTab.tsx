import { Suspense, lazy } from 'react';
import { tr } from '../../lib/i18n';
import type { WikiLinkHandler } from '../../lib/markdown';
import { ConceptsTab } from './ConceptsTab';
import { MethodsTab } from './MethodsTab';
import { GapsTab } from './GapsTab';
import { NotesTab } from './NotesTab';
import { KNOWLEDGE_VIEWS, type KnowledgeView } from './libraryTabs';

const GraphTab = lazy(() => import('./GraphTab').then((m) => ({ default: m.GraphTab })));
const DailyDigestTab = lazy(() => import('./DailyDigestTab').then((m) => ({ default: m.DailyDigestTab })));

/* ============================================================
   文献库的「知识」页签：从论文里整理出来的东西，一行安静的子视图切换。
   概念 · 方法 · 关系图 · 研究缺口 · 每日简报 · 我的笔记
   ============================================================ */


// 模块级常量存两份文案，渲染时再 tr
const LABELS: Record<KnowledgeView, { zh: string; en: string; hintZh: string; hintEn: string }> = {
  concepts: { zh: '概念', en: 'Concepts', hintZh: '论文里反复出现的问题、方法和术语', hintEn: 'Problems, methods and terms that recur across papers' },
  methods: { zh: '方法', en: 'Methods', hintZh: '各篇论文用的方法，可以并排比较', hintEn: 'The methods papers use, side by side' },
  graph: { zh: '关系图', en: 'Map', hintZh: '论文和概念之间的关系', hintEn: 'How papers and concepts connect' },
  gaps: { zh: '研究缺口', en: 'Open questions', hintZh: '论文里提到但还没解决的问题', hintEn: 'Problems the papers raise but don’t solve' },
  digest: { zh: '每日简报', en: 'Daily digest', hintZh: '每天新加入论文的简报', hintEn: 'A short brief on each day’s new papers' },
  notes: { zh: '我的笔记', en: 'My notes', hintZh: '你在这个文献库里写的全部笔记', hintEn: 'All the notes you wrote in this library' },
};

export function KnowledgeTab({
  pid,
  libraryId,
  view,
  onView,
  conceptId,
  onSelectConcept,
  onOpenPaper,
  onOpenConcept,
  onWikiLink,
  ingestRunning,
  hasWatermark,
}: {
  pid?: string;
  libraryId: string;
  view: KnowledgeView;
  onView: (v: KnowledgeView) => void;
  conceptId: string | null;
  onSelectConcept: (id: string | null) => void;
  onOpenPaper: (id: string) => void;
  onOpenConcept: (id: string) => void;
  onWikiLink: WikiLinkHandler;
  ingestRunning: boolean;
  hasWatermark: boolean;
}) {
  const fallback = <div className="skel" style={{ flex: 1, margin: 16 }} />;
  return (
    <div className="kn-wrap">
      <div className="kn-nav" role="tablist" aria-label={tr('知识', 'Knowledge')}>
        {KNOWLEDGE_VIEWS.map((v) => (
          <button
            key={v}
            type="button"
            role="tab"
            aria-selected={view === v}
            className={'pl-qtab' + (view === v ? ' is-on' : '')}
            title={tr(LABELS[v].hintZh, LABELS[v].hintEn)}
            data-testid={`kn-${v}`}
            onClick={() => onView(v)}
          >
            {tr(LABELS[v].zh, LABELS[v].en)}
          </button>
        ))}
        <span className="kn-hint">{tr(LABELS[view].hintZh, LABELS[view].hintEn)}</span>
      </div>
      <div className="kn-body">
        {view === 'concepts' ? (
          <ConceptsTab
            pid={pid}
            libraryId={libraryId}
            selectedId={conceptId}
            onSelect={onSelectConcept}
            onOpenPaper={onOpenPaper}
            onWikiLink={onWikiLink}
          />
        ) : view === 'methods' ? (
          <MethodsTab libraryId={libraryId} onOpenPaper={onOpenPaper} />
        ) : view === 'graph' ? (
          <Suspense fallback={fallback}>
            <GraphTab pid={pid} libraryId={libraryId} onOpenPaper={onOpenPaper} onOpenConcept={onOpenConcept} />
          </Suspense>
        ) : view === 'gaps' ? (
          <GapsTab libraryId={libraryId} onOpenPaper={onOpenPaper} />
        ) : view === 'digest' ? (
          <Suspense fallback={fallback}>
            <DailyDigestTab
              libraryId={libraryId}
              onOpenPaper={onOpenPaper}
              onWikiLink={onWikiLink}
              ingestRunning={ingestRunning}
              hasWatermark={hasWatermark}
            />
          </Suspense>
        ) : (
          <NotesTab pid={pid} libraryId={libraryId} />
        )}
      </div>
    </div>
  );
}
