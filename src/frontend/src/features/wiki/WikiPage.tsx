import { Suspense, lazy, useCallback, useEffect, useState } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { Icon } from '../../components/ui/Icon';
import { Segmented } from '../../components/ui/Segmented';
import { toast } from '../../components/ui/Toast';
import { api } from '../../lib/api';
import { tr } from '../../lib/i18n';
import { ExportMenu, PapersTab, type AdvSearchSeed } from './PapersTab';
import { ConceptsTab } from './ConceptsTab';
import { LibraryChatTab } from './LibraryChatTab';
import { IngestTab } from './IngestTab';
import { NotesTab } from './NotesTab';
import { GovernanceTab } from './GovernanceTab';

// 图谱与 PPT 弹窗体量大且非默认视图：按需加载
const GraphTab = lazy(() => import('./GraphTab').then((m) => ({ default: m.GraphTab })));
const PresentationModal = lazy(() =>
  import('./PresentationModal').then((m) => ({ default: m.PresentationModal })),
);

/* ============================================================
   文献库工作台（P5c 起挂在 /libraries/:id 的可管理者视图；原 /wiki 页面主体）
   Tab：论文库 / 概念库 / 图谱 / 文献对话 / 建库与同步 / 笔记，
   传入 libraryId 时追加「治理」（P6：库信息与预算 / 文献库管理员 / 重复论文）；
   pid = 库背后课题 id（数据仍走 project 作用域端点，策展人与 admin 同权放行）。
   ============================================================ */

type WikiTab = 'papers' | 'concepts' | 'graph' | 'chat' | 'ingest' | 'notes' | 'govern';

export function WikiWorkbench({ pid, libraryId }: { pid: string; libraryId?: string }) {
  const navigate = useNavigate();

  const [tab, setTab] = useState<WikiTab>('papers');
  const [presentOpen, setPresentOpen] = useState(false);
  const [paperId, setPaperId] = useState<string | null>(null);
  const [conceptId, setConceptId] = useState<string | null>(null);
  /** [[概念名]] 双链点击后待解析的概念名 */
  const [pendingConceptName, setPendingConceptName] = useState<string | null>(null);
  /** 深链带入的作者/机构筛选（seq 递增，PapersTab 据此重新应用） */
  const [advSeed, setAdvSeed] = useState<AdvSearchSeed | null>(null);

  // 切换课题/库时重置选中态
  useEffect(() => {
    setPaperId(null);
    setConceptId(null);
    setPendingConceptName(null);
  }, [pid]);

  // 深链 ?paper=<id>（idea 详情 / 阅读页返回）、?concept=<名称>
  // （阅读页双链跳转，按名称解析）、?author= / ?affiliation=
  // （阅读页作者/机构点击 → 论文库按其过滤）与 ?tab=<tab>
  // （工作台「下一步」直达建库面板）：处理后清掉参数
  const [searchParams, setSearchParams] = useSearchParams();
  useEffect(() => {
    const p = searchParams.get('paper');
    const c = searchParams.get('concept');
    const author = searchParams.get('author');
    const affiliation = searchParams.get('affiliation');
    const tabParam = searchParams.get('tab');
    if (!p && !c && !author && !affiliation && !tabParam) return;
    if (p) {
      setPaperId(p);
      setTab('papers');
    } else if (c) {
      setPendingConceptName(c);
    } else if (author || affiliation) {
      setAdvSeed((old) => ({
        author: author ?? undefined,
        affiliation: affiliation ?? undefined,
        seq: (old?.seq ?? 0) + 1,
      }));
      setTab('papers');
    } else if (tabParam && ['papers', 'concepts', 'graph', 'chat', 'ingest', 'notes', 'govern'].includes(tabParam)) {
      setTab(tabParam as WikiTab);
    }
    setSearchParams({}, { replace: true });
  }, [searchParams, setSearchParams]);

  // —— ingest 状态（tab 计数 + Ingest 面板共用） ——
  const ingestQuery = useQuery({
    queryKey: ['ingest-state', pid],
    queryFn: () => api.getIngestState(pid),
    retry: false,
    refetchInterval: (q) => (q.state.data?.running_voyage_id ? 5_000 : 60_000),
  });

  // —— [[概念名]] → 概念 id 解析 ——
  const resolveQuery = useQuery({
    queryKey: ['concept-resolve', pid, pendingConceptName],
    queryFn: () => api.listConcepts(pid, { q: pendingConceptName ?? '' }),
    enabled: !!pendingConceptName,
    retry: false,
  });
  useEffect(() => {
    if (!pendingConceptName) return;
    if (resolveQuery.isError) {
      toast(tr('概念解析失败（后端不可用）', 'Concept lookup failed (backend unavailable)'), 'error');
      setPendingConceptName(null);
      return;
    }
    if (!resolveQuery.data) return;
    const name = pendingConceptName.toLowerCase();
    const hit =
      resolveQuery.data.find((c) => c.name.toLowerCase() === name) ?? resolveQuery.data[0];
    if (hit) {
      setConceptId(hit.id);
      setTab('concepts');
    } else {
      toast(
        tr(`概念 ${pendingConceptName} 尚未入库`, `Concept ${pendingConceptName} is not in the library yet`),
        'info',
      );
    }
    setPendingConceptName(null);
  }, [pendingConceptName, resolveQuery.data, resolveQuery.isError]);

  const goPaper = useCallback((id: string) => {
    setPaperId(id);
    setTab('papers');
  }, []);
  const goConcept = useCallback((id: string) => {
    setConceptId(id);
    setTab('concepts');
  }, []);
  const onWikiLink = useCallback((name: string) => {
    setPendingConceptName(name);
  }, []);

  // 论文库计数口径 = 库内（相关性达标）；旧后端无 library 字段时退回 total
  const total = ingestQuery.data?.paper_counts?.library ?? ingestQuery.data?.paper_counts?.total;

  return (
    <>
      <div className="row" style={{ marginBottom: 14, justifyContent: 'space-between' }}>
        <Segmented<WikiTab>
          options={[
            { v: 'papers', label: `${tr('论文库', 'Papers')}${total !== undefined ? ` · ${total}` : ''}` },
            { v: 'concepts', label: tr('概念库', 'Concepts') },
            { v: 'graph', label: tr('图谱', 'Graph') },
            { v: 'chat', label: tr('文献对话', 'Chat') },
            { v: 'ingest', label: tr('建库与同步', 'Ingest & sync') },
            { v: 'notes', label: tr('笔记', 'Notes') },
            ...(libraryId ? [{ v: 'govern' as const, label: tr('治理', 'Governance') }] : []),
          ]}
          value={tab}
          onChange={setTab}
        />
        <div className="row gap8">
          {ingestQuery.data?.running_voyage_id && tab !== 'ingest' && (
            <span
              className="pill hoverable"
              style={{ background: 'var(--ok-bg)', color: 'var(--ok-tx)' }}
              onClick={() => navigate(`/voyages/${ingestQuery.data?.running_voyage_id ?? ''}`)}
            >
              <span className="dot pulse" />
              {tr('文献任务运行中 →', 'Literature task running →')}
            </span>
          )}
          <button className="btn btn-ghost sm" onClick={() => setPresentOpen(true)}>
            <Icon name="chart" size={13} />
            {tr('论文分享 PPT', 'Paper sharing PPT')}
          </button>
          <ExportMenu pid={pid} />
        </div>
      </div>

      <div
        className="card"
        style={{
          overflow: 'hidden',
          display: 'flex',
          flexDirection: 'column',
          flex: 1,
          minHeight: 480,
        }}
      >
        {tab === 'papers' ? (
          <PapersTab
            pid={pid}
            selectedId={paperId}
            onSelect={setPaperId}
            onOpenConcept={goConcept}
            onWikiLink={onWikiLink}
            advSeed={advSeed}
          />
        ) : tab === 'concepts' ? (
          <ConceptsTab
            pid={pid}
            selectedId={conceptId}
            onSelect={setConceptId}
            onOpenPaper={goPaper}
            onWikiLink={onWikiLink}
          />
        ) : tab === 'graph' ? (
          <Suspense fallback={<div className="skel" style={{ flex: 1, margin: 16 }} />}>
            <GraphTab pid={pid} onOpenPaper={goPaper} onOpenConcept={goConcept} />
          </Suspense>
        ) : tab === 'chat' ? (
          <LibraryChatTab pid={pid} onOpenPaper={goPaper} onWikiLink={onWikiLink} />
        ) : tab === 'ingest' ? (
          <IngestTab
            pid={pid}
            state={ingestQuery.data}
            stateError={ingestQuery.isError}
            stateLoading={ingestQuery.isLoading}
          />
        ) : tab === 'govern' && libraryId ? (
          <GovernanceTab libraryId={libraryId} />
        ) : (
          <NotesTab pid={pid} />
        )}
      </div>

      {presentOpen && (
        <Suspense fallback={null}>
          <PresentationModal
            projectId={pid}
            initialPaperId={paperId ?? undefined}
            onClose={() => setPresentOpen(false)}
          />
        </Suspense>
      )}
    </>
  );
}
