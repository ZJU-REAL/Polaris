import { Suspense, lazy, useCallback, useEffect, useMemo, useState } from 'react';
import { Link, useNavigate, useSearchParams } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { Icon } from '../../components/ui/Icon';
import { Drawer } from '../../components/ui/Drawer';
import { DotsMenu } from '../../components/ui/DotsMenu';
import { toast } from '../../components/ui/Toast';
import { api, type DirectionLibraryDetail } from '../../lib/api';
import { parseApiDate } from '../../lib/format';
import { tr } from '../../lib/i18n';
import { useDirections } from '../deepresearch/directions';
import { useLibraryExport, PapersTab, PapersTrashModal, type AdvSearchSeed } from './PapersTab';
import { AddPapersModal } from './AddPapersModal';
import { pickConceptByName } from './shared';
import { GovernanceTab } from './GovernanceTab';
import { FindPapersTab } from './FindPapersTab';
import { KnowledgeTab } from './KnowledgeTab';
import { AskTab } from './AskTab';
import { normalizeLibraryParams, readLibraryParams, type LibraryTab } from './libraryTabs';
import { ExtensionBatchHistoryModal } from './ExtensionBatchHistoryModal';
import { extensionDeliveryAvailable } from '../../lib/polaris-extension';
import '../../app/DirectionNav.css';
import './workbench.css';

const PresentationModal = lazy(() =>
  import('./PresentationModal').then((m) => ({ default: m.PresentationModal })),
);

/* ============================================================
   文献库页（/libraries/:id）。四个页签，其余收进设置抽屉和「更多」：

     论文   列表 + 详情（默认）
     找论文 关键词检索 / 按编号或 BibTeX / Zotero / 参考文献扩展，一份记录
     知识   概念 · 方法 · 图谱 · 研究缺口 · 每日简报 · 我的笔记
     问答   快速对话 / 深入回答

   页签和子视图都写在地址里（?tab=find、?tab=knowledge&view=graph、?tab=ask&mode=thorough），
   刷新、后退都回到原处。旧地址（?tab=discover / ingest / concepts / chat / qa / govern …）
   换成新的再显示，见 LEGACY_TABS。
   ============================================================ */

const RUN_DOT = { width: 8, height: 8, borderRadius: '50%', background: 'var(--ok)', display: 'inline-block', flexShrink: 0 } as const;

export function WikiWorkbench({ lib }: { lib: DirectionLibraryDetail }) {
  const navigate = useNavigate();
  const libraryId = lib.id;
  const pid = lib.project_id ?? undefined;
  const { byLibrary, byProject } = useDirections();
  const direction = byLibrary(libraryId) ?? byProject(pid);

  const [searchParams, setSearchParams] = useSearchParams();

  // —— 旧地址换新地址 ——
  useEffect(() => {
    const next = normalizeLibraryParams(searchParams);
    if (next) setSearchParams(next, { replace: true });
  }, [searchParams, setSearchParams]);

  const { tab, view, mode: askMode, settings: settingsOpen, paper: paperId } = readLibraryParams(searchParams);

  /** 改地址里的几项，其余原样留着。push=true 时进历史（换页签），否则替换（选中论文）。 */
  const patchParams = useCallback(
    (patch: Record<string, string | null>, push = false) => {
      setSearchParams(
        (prev) => {
          const next = new URLSearchParams(prev);
          for (const [k, v] of Object.entries(patch)) {
            if (v === null || v === '') next.delete(k);
            else next.set(k, v);
          }
          return next;
        },
        { replace: !push },
      );
    },
    [setSearchParams],
  );

  const setTab = (t: LibraryTab) =>
    patchParams({ tab: t === 'papers' ? null : t, view: t === 'knowledge' ? view : null, mode: t === 'ask' ? askMode : null }, true);
  const setPaperId = useCallback((id: string) => patchParams({ paper: id || null }), [patchParams]);
  const openSettings = (open: boolean) => patchParams({ settings: open ? '1' : null }, open);

  const [conceptId, setConceptId] = useState<string | null>(null);
  const [pendingConceptName, setPendingConceptName] = useState<string | null>(null);
  const [advSeed, setAdvSeed] = useState<AdvSearchSeed | null>(null);
  const [addOpen, setAddOpen] = useState(false);
  const [trashOpen, setTrashOpen] = useState(false);
  const [extensionHistoryOpen, setExtensionHistoryOpen] = useState(false);
  const [presentOpen, setPresentOpen] = useState(false);

  useEffect(() => {
    setConceptId(null);
    setPendingConceptName(null);
  }, [libraryId]);

  // 一次性的深链：?concept=<名称>、?conceptId=、?author= / ?affiliation=。处理完从地址里拿掉。
  useEffect(() => {
    const c = searchParams.get('concept');
    const cid = searchParams.get('conceptId');
    const author = searchParams.get('author');
    const affiliation = searchParams.get('affiliation');
    if (!c && !cid && !author && !affiliation) return;
    if (cid) {
      setConceptId(cid);
      patchParams({ conceptId: null, tab: 'knowledge', view: 'concepts' });
      return;
    }
    if (c) setPendingConceptName(c);
    if (author || affiliation) {
      setAdvSeed((old) => ({ author: author ?? undefined, affiliation: affiliation ?? undefined, seq: (old?.seq ?? 0) + 1 }));
    }
    patchParams({ concept: null, author: null, affiliation: null, ...(author || affiliation ? { tab: null } : {}) });
  }, [searchParams, patchParams]);

  const ingestQuery = useQuery({
    queryKey: ['ingest-state', libraryId],
    queryFn: () => api.getLibraryIngestState(libraryId),
    retry: false,
    refetchInterval: (q) => (q.state.data?.running_voyage_id ? 5_000 : 60_000),
  });

  // [[概念名]] → 概念 id
  const resolveQuery = useQuery({
    queryKey: ['concept-resolve', pendingConceptName],
    queryFn: () => api.lookupConcept(pendingConceptName ?? ''),
    enabled: !!pendingConceptName,
    retry: false,
  });
  useEffect(() => {
    if (!pendingConceptName) return;
    if (resolveQuery.isError) {
      toast(tr('无法查找概念，请确认本机引擎正在运行', 'Couldn’t look up the concept. Check that the local engine is running.'), 'error');
      setPendingConceptName(null);
      return;
    }
    if (!resolveQuery.data) return;
    const hit = pickConceptByName(resolveQuery.data, pendingConceptName);
    if (hit) {
      setConceptId(hit.id);
      patchParams({ tab: 'knowledge', view: 'concepts' }, true);
    } else {
      toast(tr(`还没有概念「${pendingConceptName}」`, `No concept named “${pendingConceptName}” yet`), 'info');
    }
    setPendingConceptName(null);
  }, [pendingConceptName, resolveQuery.data, resolveQuery.isError, patchParams]);

  const goPaper = useCallback((id: string) => patchParams({ tab: null, view: null, mode: null, paper: id }, true), [patchParams]);
  const goConcept = useCallback(
    (id: string) => {
      setConceptId(id);
      patchParams({ tab: 'knowledge', view: 'concepts' }, true);
    },
    [patchParams],
  );
  const onWikiLink = useCallback((name: string) => setPendingConceptName(name), []);

  const counts = ingestQuery.data?.paper_counts;
  const total = counts?.library ?? counts?.total ?? lib.paper_count;
  const summarised = counts?.compiled;

  const exportLib = useLibraryExport(libraryId);

  const meta = useMemo(() => {
    const parts: string[] = [];
    if (total !== undefined) parts.push(tr(`${total} 篇论文`, total === 1 ? '1 paper' : `${total} papers`));
    if (summarised) parts.push(tr(`${summarised} 篇有解读`, `${summarised} with a summary`));
    const updated = lib.last_synced_at ?? lib.updated_at;
    if (updated) {
      const d = parseApiDate(updated);
      parts.push(
        tr(
          `${d.getMonth() + 1} 月 ${d.getDate()} 日更新`,
          `Updated ${d.toLocaleDateString('en', { month: 'short', day: 'numeric' })}`,
        ),
      );
    }
    return parts.join(' · ');
  }, [total, summarised, lib.last_synced_at, lib.updated_at]);

  const running = ingestQuery.data?.running_voyage_id;
  const tabs: { key: LibraryTab; label: string; count?: number }[] = [
    { key: 'papers', label: tr('论文', 'Papers'), count: total },
    { key: 'find', label: tr('找论文', 'Find papers') },
    { key: 'knowledge', label: tr('知识', 'Knowledge') },
    { key: 'ask', label: tr('问答', 'Ask') },
  ];

  return (
    <>
      <header className="dn lw-head">
        <div className="dn-top">
          <h1 className="dn-title" title={lib.name}>
            {lib.name}
          </h1>
          <div className="dn-actions lw-actions">
            {running && (
              ingestQuery.data?.can_open_running_voyage ? (
                <button type="button" className="btn btn-plain sm" onClick={() => navigate(`/voyages/${running}`)}>
                  <span className="pulse" style={RUN_DOT} />
                  {tr('正在找论文', 'Finding papers')}
                </button>
              ) : (
                <span className="lw-running">
                  <span className="pulse" style={RUN_DOT} />
                  {tr('正在找论文', 'Finding papers')}
                </span>
              )
            )}
            <button type="button" className="btn btn-ghost sm" onClick={() => setAddOpen(true)} data-testid="lib-add">
              <Icon name="plus" size={13} />
              <span className="lw-hide-narrow">{tr('添加论文', 'Add papers')}</span>
            </button>
            <button
              type="button"
              className="btn btn-plain sm"
              onClick={() => openSettings(true)}
              title={tr('文献库设置', 'Library settings')}
              data-testid="lib-settings"
            >
              <Icon name="settings" size={14} />
              <span className="lw-hide-narrow">{tr('设置', 'Settings')}</span>
            </button>
            <DotsMenu
              items={[
                ...exportLib.items,
                ...(pid
                  ? [{ key: 'ppt', label: tr('做成论文分享 PPT', 'Make paper slides'), icon: 'chart' as const, separated: true, onSelect: () => setPresentOpen(true) }]
                  : []),
                ...(extensionDeliveryAvailable()
                  ? [{ key: 'ext', label: tr('浏览器扩展下载记录', 'Browser extension downloads'), icon: 'clock' as const, onSelect: () => setExtensionHistoryOpen(true) }]
                  : []),
                { key: 'trash', label: tr('回收站', 'Trash'), icon: 'trash' as const, separated: !pid, onSelect: () => setTrashOpen(true) },
              ]}
            />
          </div>
        </div>
        <div className="lw-meta">
          {meta}
          {direction && (
            <>
              {meta && <span aria-hidden> · </span>}
              <Link to={direction.href} className="lw-topic">
                {tr('课题', 'Topic')}
                {direction.name !== lib.name ? `「${direction.name}」` : ''}
                <Icon name="arrow" size={11} />
              </Link>
            </>
          )}
        </div>
        <nav className="dn-tabs" role="tablist" aria-label={tr('文献库', 'Library')}>
          {tabs.map((t) => (
            <button
              key={t.key}
              type="button"
              role="tab"
              aria-selected={t.key === tab}
              className={'dn-tab' + (t.key === tab ? ' is-active' : '')}
              data-testid={`lib-tab-${t.key}`}
              onClick={() => setTab(t.key)}
            >
              {t.label}
              {t.count !== undefined && <span className="lw-count">{t.count}</span>}
            </button>
          ))}
        </nav>
      </header>

      <div className="card literature-workspace-card lw-body">
        {tab === 'find' ? (
          <FindPapersTab
            lib={lib}
            state={ingestQuery.data}
            stateError={ingestQuery.isError}
            direction={direction}
            onOpenPaper={goPaper}
            onOpenSettings={() => openSettings(true)}
          />
        ) : tab === 'knowledge' ? (
          <KnowledgeTab
            pid={pid}
            libraryId={libraryId}
            view={view}
            onView={(v) => patchParams({ view: v }, true)}
            conceptId={conceptId}
            onSelectConcept={setConceptId}
            onOpenPaper={goPaper}
            onOpenConcept={goConcept}
            onWikiLink={onWikiLink}
            ingestRunning={!!running}
            hasWatermark={!!ingestQuery.data?.watermark}
          />
        ) : tab === 'ask' ? (
          <AskTab
            pid={pid}
            libraryId={libraryId}
            mode={askMode}
            onMode={(m) => patchParams({ mode: m })}
            onOpenPaper={goPaper}
            onWikiLink={onWikiLink}
          />
        ) : (
          <PapersTab
            pid={pid}
            libraryId={libraryId}
            selectedId={paperId}
            onSelect={setPaperId}
            onOpenConcept={goConcept}
            onWikiLink={onWikiLink}
            advSeed={advSeed}
            onFindPapers={() => setTab('find')}
          />
        )}
      </div>

      <Drawer
        open={settingsOpen}
        onClose={() => openSettings(false)}
        title={<span style={{ fontSize: 'var(--fs-title)', fontWeight: 600 }}>{tr('文献库设置', 'Library settings')}</span>}
        sub={tr('名称、收录范围和重复论文', 'Name, what to collect, and duplicates')}
        width={640}
      >
        <GovernanceTab libraryId={libraryId} embedded />
      </Drawer>

      <AddPapersModal libraryId={libraryId} open={addOpen} onClose={() => setAddOpen(false)} onImported={goPaper} />
      <PapersTrashModal pid={pid ?? ''} libraryId={libraryId} open={trashOpen} onClose={() => setTrashOpen(false)} />
      {extensionDeliveryAvailable() && (
        <ExtensionBatchHistoryModal libraryId={libraryId} open={extensionHistoryOpen} onClose={() => setExtensionHistoryOpen(false)} />
      )}
      {pid && presentOpen && (
        <Suspense fallback={null}>
          <PresentationModal projectId={pid} initialPaperId={paperId ?? undefined} onClose={() => setPresentOpen(false)} />
        </Suspense>
      )}
    </>
  );
}
