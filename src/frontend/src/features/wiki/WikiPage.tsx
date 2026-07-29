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
import { pickConceptByName } from './shared';
import { LibraryChatTab } from './LibraryChatTab';
import { IngestTab } from './IngestTab';
import { NotesTab } from './NotesTab';
import { GovernanceTab } from './GovernanceTab';

// 图谱与 PPT 弹窗体量大且非默认视图：按需加载
const GraphTab = lazy(() => import('./GraphTab').then((m) => ({ default: m.GraphTab })));
const DailyDigestTab = lazy(() =>
  import('./DailyDigestTab').then((m) => ({ default: m.DailyDigestTab })),
);
const PresentationModal = lazy(() =>
  import('./PresentationModal').then((m) => ({ default: m.PresentationModal })),
);

/* ============================================================
   文献库工作台（P5c 起挂在 /libraries/:id 的可管理者视图；原 /wiki 页面主体）
   Tab：论文库 / 概念库 / 图谱 / 文献对话 / 建库与同步 / 笔记，
   传入 libraryId 时追加治理（P6：库信息与预算 / 文献库管理员 / 重复论文）；
   数据一律走 /libraries/{id}/* 端点（策展人与 admin 放行）。pid 只是这个库当初
   从哪个课题建的，如今仅用来决定要不要显示课题域的 PPT。
   ============================================================ */

type WikiTab = 'papers' | 'concepts' | 'graph' | 'digest' | 'chat' | 'ingest' | 'notes' | 'govern';

export function WikiWorkbench({
  pid,
  libraryId,
  canManage = false,
}: {
  pid?: string;
  libraryId?: string;
  /** 能否管理这个库（决定共享 Tab 里的管理操作显不显示）；由调用方按 can_manage 传入。 */
  canManage?: boolean;
}) {
  const navigate = useNavigate();

  // 集合级数据与导出一律走 /libraries/{id}/* 端点——库已与课题解耦，pid 只是
  // 这个库当初从哪个课题建的，不再决定数据口径。
  const libScope = !!libraryId;
  const scopeId = libraryId ?? pid!;
  /** 传给各 Tab 的库作用域标识：有库就置位。 */
  const tabLibraryId = libraryId;
  /** PPT 仍是课题域功能（要课题的研究方案做叙事），只对有起源课题的库显示。 */
  const hasTopic = !!pid;

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
  }, [scopeId]);

  // 深链 ?paper=<id>（idea 详情 / 阅读页返回）、?concept=<名称>
  // （阅读页双链跳转，按名称解析）、?conceptId=<id>（实验室跨库图谱点概念直接进来）、
  // ?author= / ?affiliation=（阅读页作者/机构点击 → 论文库按其过滤）与 ?tab=<tab>
  // （工作台「下一步」直达建库面板）：处理后清掉参数
  const [searchParams, setSearchParams] = useSearchParams();
  useEffect(() => {
    const p = searchParams.get('paper');
    const c = searchParams.get('concept');
    const cid = searchParams.get('conceptId');
    const author = searchParams.get('author');
    const affiliation = searchParams.get('affiliation');
    const tabParam = searchParams.get('tab');
    if (!p && !c && !cid && !author && !affiliation && !tabParam) return;
    if (p) {
      setPaperId(p);
      setTab('papers');
    } else if (cid) {
      setConceptId(cid);
      setTab('concepts');
    } else if (c) {
      setPendingConceptName(c);
    } else if (author || affiliation) {
      setAdvSeed((old) => ({
        author: author ?? undefined,
        affiliation: affiliation ?? undefined,
        seq: (old?.seq ?? 0) + 1,
      }));
      setTab('papers');
    } else if (tabParam && ['papers', 'concepts', 'graph', 'digest', 'chat', 'ingest', 'notes', 'govern'].includes(tabParam)) {
      setTab(tabParam as WikiTab);
    }
    setSearchParams({}, { replace: true });
  }, [searchParams, setSearchParams]);

  // —— ingest 状态（tab 计数 + Ingest 面板共用） ——
  const ingestQuery = useQuery({
    queryKey: ['ingest-state', scopeId],
    queryFn: () => (libScope ? api.getLibraryIngestState(libraryId!) : api.getIngestState(pid!)),
    retry: false,
    refetchInterval: (q) => (q.state.data?.running_voyage_id ? 5_000 : 60_000),
  });

  // —— [[概念名]] → 概念 id 解析 ——
  // 概念全平台一份，一律走平台级按名查（不限本库）；命中后仍留在当前库视角展示。
  const resolveQuery = useQuery({
    queryKey: ['concept-resolve', pendingConceptName],
    queryFn: () => api.lookupConcept(pendingConceptName ?? ''),
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
    const hit = pickConceptByName(resolveQuery.data, pendingConceptName);
    if (hit) {
      setConceptId(hit.id);
      setTab('concepts');
    } else {
      toast(
        tr(`概念「${pendingConceptName}」还没入库`, `“${pendingConceptName}” is not in the knowledge base yet`),
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
            ...(libraryId ? [{ v: 'digest' as const, label: tr('每日简报', 'Daily digest') }] : []),
            { v: 'chat', label: tr('文献对话', 'Chat') },
            { v: 'notes', label: tr('笔记', 'Notes') },
            ...(libraryId ? [{ v: 'govern' as const, label: tr('文献库配置', 'Library config') }] : []),
            // 建库与同步放到最后一个标签
            { v: 'ingest', label: tr('建库与同步', 'Ingest & sync') },
          ]}
          value={tab}
          onChange={setTab}
        />
        <div className="row gap8">
          {/* 无权打开任务详情时只报状态、不给跳转（点了会 404） */}
          {ingestQuery.data?.running_voyage_id && tab !== 'ingest' && (
            ingestQuery.data.can_open_running_voyage ? (
              <span
                className="pill hoverable"
                style={{ background: 'var(--ok-bg)', color: 'var(--ok-tx)' }}
                onClick={() => navigate(`/voyages/${ingestQuery.data?.running_voyage_id ?? ''}`)}
              >
                <span className="dot pulse" />
                {tr('文献任务运行中 →', 'Literature task running →')}
              </span>
            ) : (
              <span className="pill" style={{ background: 'var(--ok-bg)', color: 'var(--ok-tx)' }}>
                <span className="dot pulse" />
                {tr('文献任务运行中', 'Literature task running')}
              </span>
            )
          )}
          {hasTopic && (
            <button className="btn btn-ghost sm" onClick={() => setPresentOpen(true)}>
              <Icon name="chart" size={13} />
              {tr('论文分享 PPT', 'Paper sharing PPT')}
            </button>
          )}
          {/* 导出走库作用域端点；没有库时（理论上不会发生）回落课题端点 */}
          <ExportMenu pid={pid} libraryId={tabLibraryId} />
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
            libraryId={tabLibraryId}
            selectedId={paperId}
            onSelect={setPaperId}
            onOpenConcept={goConcept}
            onWikiLink={onWikiLink}
            advSeed={advSeed}
          />
        ) : tab === 'concepts' ? (
          <ConceptsTab
            pid={pid}
            libraryId={tabLibraryId}
            canManage={canManage}
            selectedId={conceptId}
            onSelect={setConceptId}
            onOpenPaper={goPaper}
            onWikiLink={onWikiLink}
          />
        ) : tab === 'graph' ? (
          <Suspense fallback={<div className="skel" style={{ flex: 1, margin: 16 }} />}>
            <GraphTab pid={pid} libraryId={tabLibraryId} onOpenPaper={goPaper} onOpenConcept={goConcept} />
          </Suspense>
        ) : tab === 'digest' && libraryId ? (
          <Suspense fallback={<div className="skel" style={{ flex: 1, margin: 16 }} />}>
            <DailyDigestTab
              libraryId={libraryId}
              onOpenPaper={goPaper}
              onWikiLink={onWikiLink}
              canGenerate={canManage}
              ingestRunning={!!ingestQuery.data?.running_voyage_id}
              hasWatermark={!!ingestQuery.data?.watermark}
            />
          </Suspense>
        ) : tab === 'chat' ? (
          <LibraryChatTab
            pid={pid}
            libraryId={tabLibraryId}
            canManage={canManage}
            onOpenPaper={goPaper}
            onWikiLink={onWikiLink}
          />
        ) : tab === 'ingest' ? (
          <IngestTab
            pid={pid}
            libraryId={tabLibraryId}
            state={ingestQuery.data}
            stateError={ingestQuery.isError}
            stateLoading={ingestQuery.isLoading}
            onGoGovern={libraryId ? () => setTab('govern') : undefined}
          />
        ) : tab === 'govern' && libraryId ? (
          <GovernanceTab libraryId={libraryId} />
        ) : (
          <NotesTab pid={pid} libraryId={tabLibraryId} />
        )}
      </div>

      {hasTopic && presentOpen && (
        <Suspense fallback={null}>
          <PresentationModal
            projectId={pid!}
            initialPaperId={paperId ?? undefined}
            onClose={() => setPresentOpen(false)}
          />
        </Suspense>
      )}
    </>
  );
}
