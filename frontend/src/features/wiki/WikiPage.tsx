import { useCallback, useEffect, useState } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { useMutation, useQuery } from '@tanstack/react-query';
import { Icon } from '../../components/ui/Icon';
import { PageHead } from '../../components/ui/PageHead';
import { Segmented } from '../../components/ui/Segmented';
import { EmptyState } from '../../components/ui/EmptyState';
import { toast } from '../../components/ui/Toast';
import { useProject } from '../../app/project';
import { api } from '../../lib/api';
import { PapersTab } from './PapersTab';
import { ConceptsTab } from './ConceptsTab';
import { IngestTab } from './IngestTab';

/* ============================================================
   /wiki — Research Wiki 文献调研页（M2）
   三个 Tab：论文库 / 概念库 / 冷启动·同步；
   顶部当前研究方向 + Obsidian 导出。
   ============================================================ */

type WikiTab = 'papers' | 'concepts' | 'ingest';

export function WikiPage() {
  const navigate = useNavigate();
  const { projects, isLoading: projectsLoading, currentProject, currentProjectId } = useProject();
  const pid = currentProjectId;

  const [tab, setTab] = useState<WikiTab>('papers');
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

  // 深链 /wiki?paper=<id>（idea 详情的 parent paper 跳转）：选中后清掉参数
  const [searchParams, setSearchParams] = useSearchParams();
  useEffect(() => {
    const p = searchParams.get('paper');
    if (!p) return;
    setPaperId(p);
    setTab('papers');
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
      toast(`概念「${pendingConceptName}」尚未入库`, 'info');
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

  // —— Obsidian 导出 ——
  const exportMutation = useMutation({
    mutationFn: () => api.downloadObsidianExport(pid!),
    onSuccess: (blob) => {
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `polaris-wiki-${(currentProject?.name ?? 'vault').replace(/\s+/g, '-')}.zip`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
      toast('Obsidian vault 已导出', 'ok');
    },
    onError: (e) => toast(`导出失败：${e instanceof Error ? e.message : String(e)}`, 'error'),
  });

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
            desc="Research Wiki 按研究方向组织：先通过结构化访谈创建一个方向，再运行冷启动回填文献。"
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

  const total = ingestQuery.data?.paper_counts?.total;

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
            <button
              className="btn btn-ghost"
              disabled={!pid || exportMutation.isPending}
              onClick={() => exportMutation.mutate()}
            >
              {exportMutation.isPending ? (
                <Icon name="refresh" size={14} style={{ animation: 'spin 1s linear infinite' }} />
              ) : (
                <Icon name="file" size={14} />
              )}
              导出 Obsidian
            </button>
          </>
        }
      />

      <div className="row" style={{ marginBottom: 14, justifyContent: 'space-between' }}>
        <Segmented<WikiTab>
          options={[
            { v: 'papers', label: `论文库 Papers${total !== undefined ? ` · ${total}` : ''}` },
            { v: 'concepts', label: '概念库 Concepts' },
            { v: 'ingest', label: '冷启动 / 同步 Ingest' },
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
        ) : (
          <IngestTab
            pid={pid}
            state={ingestQuery.data}
            stateError={ingestQuery.isError}
            stateLoading={ingestQuery.isLoading}
          />
        )}
      </div>
    </div>
  );
}
