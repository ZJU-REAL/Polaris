import { useCallback, useEffect, useState } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { Icon } from '../../components/ui/Icon';
import { PageHead } from '../../components/ui/PageHead';
import { Segmented } from '../../components/ui/Segmented';
import { EmptyState } from '../../components/ui/EmptyState';
import { toast } from '../../components/ui/Toast';
import { useProject } from '../../app/project';
import { api } from '../../lib/api';
import { ExportMenu, PapersTab } from './PapersTab';
import { PresentationModal } from './PresentationModal';
import { ConceptsTab } from './ConceptsTab';
import { GraphTab } from './GraphTab';
import { LibraryChatTab } from './LibraryChatTab';
import { IngestTab } from './IngestTab';
import { NotesTab } from './NotesTab';

/* ============================================================
   /wiki — Research Wiki 文献调研页（M2 + 文献管理增强）
   四个 Tab：论文库 / 概念库 / 建库与同步 / 笔记本；
   顶部当前研究方向 + Obsidian 导出。
   ============================================================ */

type WikiTab = 'papers' | 'concepts' | 'graph' | 'chat' | 'ingest' | 'notes';

export function WikiPage() {
  const navigate = useNavigate();
  const { projects, isLoading: projectsLoading, currentProject, currentProjectId } = useProject();
  const pid = currentProjectId;

  const [tab, setTab] = useState<WikiTab>('papers');
  const [presentOpen, setPresentOpen] = useState(false);
  const [paperId, setPaperId] = useState<string | null>(null);
  const [conceptId, setConceptId] = useState<string | null>(null);
  /** [[概念名]] 双链点击后待解析的概念名 */
  const [pendingConceptName, setPendingConceptName] = useState<string | null>(null);

  // 切换项目时重置选中态
  useEffect(() => {
    setPaperId(null);
    setConceptId(null);
    setPendingConceptName(null);
  }, [pid]);

  // 深链 /wiki?paper=<id>（idea 详情 / 阅读页返回）与 /wiki?concept=<名称>
  // （阅读页双链跳转，按名称解析）：处理后清掉参数
  const [searchParams, setSearchParams] = useSearchParams();
  useEffect(() => {
    const p = searchParams.get('paper');
    const c = searchParams.get('concept');
    if (!p && !c) return;
    if (p) {
      setPaperId(p);
      setTab('papers');
    } else if (c) {
      setPendingConceptName(c);
    }
    setSearchParams({}, { replace: true });
  }, [searchParams, setSearchParams]);

  // —— ingest 状态（tab 计数 + Ingest 面板共用） ——
  const ingestQuery = useQuery({
    queryKey: ['ingest-state', pid],
    queryFn: () => api.getIngestState(pid!),
    enabled: !!pid,
    retry: false,
    refetchInterval: (q) => (q.state.data?.running_voyage_id ? 5_000 : 60_000),
  });

  // —— [[概念名]] → 概念 id 解析 ——
  const resolveQuery = useQuery({
    queryKey: ['concept-resolve', pid, pendingConceptName],
    queryFn: () => api.listConcepts(pid!, { q: pendingConceptName ?? '' }),
    enabled: !!pid && !!pendingConceptName,
    retry: false,
  });
  useEffect(() => {
    if (!pendingConceptName) return;
    if (resolveQuery.isError) {
      toast('概念解析失败（后端不可用）', 'error');
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
      toast(`概念 ${pendingConceptName} 尚未入库`, 'info');
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

  // —— 无项目：引导创建 ——
  if (!projectsLoading && projects.length === 0) {
    return (
      <div className="page fadeup">
        <PageHead
          eyebrow="Stage 00 · Research Wiki"
          title="文献调研 Research Wiki"
          sub="每日自动抓取、打分、精读编译前沿文献，知识库复利增长。"
        />
        <div className="card">
          <EmptyState
            icon="book"
            title="还没有研究方向"
            desc="Research Wiki 按研究方向组织：先通过结构化访谈创建一个方向，再运行初始建库回填文献。"
            action={
              <button className="btn btn-primary" onClick={() => navigate('/projects/new')}>
                <Icon name="plus" size={14} />
                新建研究方向 · New direction
              </button>
            }
          />
        </div>
      </div>
    );
  }

  // 论文库计数口径 = 库内（相关性达标）；旧后端无 library 字段时退回 total
  const total = ingestQuery.data?.paper_counts?.library ?? ingestQuery.data?.paper_counts?.total;

  return (
    <div className="page fadeup" style={{ maxWidth: 1360, display: 'flex', flexDirection: 'column', height: '100%', paddingBottom: 24 }}>
      <PageHead
        eyebrow="Stage 00 · Research Wiki"
        title="文献调研 Research Wiki"
        sub={
          currentProject
            ? `当前方向：${currentProject.name}`
            : projectsLoading
              ? '加载研究方向…'
              : '选择一个研究方向'
        }
        en="papers · concepts · ingest"
        right={
          <>
            {currentProject && (
              <button
                className="btn btn-ghost"
                onClick={() => navigate(`/projects/${currentProject.id}`)}
              >
                <Icon name="compass" size={14} />
                方向详情
              </button>
            )}
            <button className="btn btn-ghost" disabled={!pid} onClick={() => setPresentOpen(true)}>
              <Icon name="chart" size={14} />
              论文分享 PPT
            </button>
            {pid && <ExportMenu pid={pid} />}
          </>
        }
      />

      <div className="row" style={{ marginBottom: 14, justifyContent: 'space-between' }}>
        <Segmented<WikiTab>
          options={[
            { v: 'papers', label: `论文库 Papers${total !== undefined ? ` · ${total}` : ''}` },
            { v: 'concepts', label: '概念库 Concepts' },
            { v: 'graph', label: '图谱 Graph' },
            { v: 'chat', label: '文献对话 Chat' },
            { v: 'ingest', label: '建库与同步 Ingest' },
            { v: 'notes', label: '笔记 Notes' },
          ]}
          value={tab}
          onChange={setTab}
        />
        {ingestQuery.data?.running_voyage_id && tab !== 'ingest' && (
          <span
            className="pill hoverable"
            style={{ background: 'var(--ok-bg)', color: 'var(--ok-tx)' }}
            onClick={() => navigate(`/voyages/${ingestQuery.data?.running_voyage_id ?? ''}`)}
          >
            <span className="dot pulse" />
            文献任务运行中 →
          </span>
        )}
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
        {!pid ? (
          <div className="empty" style={{ margin: 'auto' }}>
            {projectsLoading ? '加载研究方向…' : '请先选择研究方向'}
          </div>
        ) : tab === 'papers' ? (
          <PapersTab
            pid={pid}
            selectedId={paperId}
            onSelect={setPaperId}
            onOpenConcept={goConcept}
            onWikiLink={onWikiLink}
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
          <GraphTab pid={pid} onOpenPaper={goPaper} onOpenConcept={goConcept} />
        ) : tab === 'chat' ? (
          <LibraryChatTab pid={pid} onOpenPaper={goPaper} onWikiLink={onWikiLink} />
        ) : tab === 'ingest' ? (
          <IngestTab
            pid={pid}
            state={ingestQuery.data}
            stateError={ingestQuery.isError}
            stateLoading={ingestQuery.isLoading}
          />
        ) : (
          <NotesTab pid={pid} />
        )}
      </div>

      {presentOpen && pid && (
        <PresentationModal
          projectId={pid}
          initialPaperId={paperId ?? undefined}
          onClose={() => setPresentOpen(false)}
        />
      )}
    </div>
  );
}
