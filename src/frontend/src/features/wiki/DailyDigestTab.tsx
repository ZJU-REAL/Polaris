import { useEffect, useMemo, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useNavigate } from 'react-router-dom';
import { EmptyState } from '../../components/ui/EmptyState';
import { Icon } from '../../components/ui/Icon';
import { Segmented } from '../../components/ui/Segmented';
import { toast } from '../../components/ui/Toast';
import { api, ApiError, type LibraryDigestCounts } from '../../lib/api';
import { tr } from '../../lib/i18n';
import { Markdown } from '../../lib/markdown';
import { useIsCompact } from '../../lib/useBreakpoint';

type DigestView = 'brief' | 'trends';

export interface DailyDigestTabProps {
  libraryId: string;
  onOpenPaper: (paperId: string) => void;
  onWikiLink: (name: string) => void;
  canGenerate?: boolean;
  ingestRunning?: boolean;
  hasWatermark?: boolean;
}

function displayDate(value: string): string {
  const parsed = new Date(`${value}T00:00:00Z`);
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleDateString();
}

function CountCard({ label, value }: { label: string; value: number }) {
  return (
    <div
      style={{
        minWidth: 104,
        flex: '1 1 104px',
        padding: '10px 12px',
        borderRadius: 9,
        background: 'var(--surface-2)',
        border: '0.5px solid var(--border-2)',
      }}
    >
      <div style={{ fontSize: 11.5, color: 'var(--text-3)' }}>{label}</div>
      <div style={{ marginTop: 3, fontSize: 20, fontWeight: 720, color: 'var(--text)' }}>{value}</div>
    </div>
  );
}

function normalizeCounts(counts: Partial<LibraryDigestCounts> | undefined): LibraryDigestCounts {
  return {
    source_fetched: counts?.source_fetched ?? 0,
    prescreened: counts?.prescreened ?? 0,
    inserted: counts?.inserted ?? 0,
    kept: counts?.kept ?? 0,
    excluded: counts?.excluded ?? 0,
    compiled: counts?.compiled ?? 0,
  };
}

/** 文献库每日简报：左侧历史，右侧本次简报 / 该时点滚动趋势。 */
export function DailyDigestTab({
  libraryId,
  onOpenPaper,
  onWikiLink,
  canGenerate = false,
  ingestRunning = false,
  hasWatermark = false,
}: DailyDigestTabProps) {
  const compact = useIsCompact();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [view, setView] = useState<DigestView>('brief');

  const generateMutation = useMutation({
    mutationFn: () => api.generateLibraryDigest(libraryId),
    onSuccess: (result) => {
      toast(
        result.strategy === 'digest_only'
          ? tr(
              `检测到今日已有 ${result.paper_count} 篇论文更新，已跳过增量同步并直接生成简报。`,
              `Found ${result.paper_count} papers updated today. Incremental sync was skipped and the digest is being generated directly.`,
            )
          : tr(
              '今日尚无论文更新，已启动增量同步；完成后会自动生成简报与趋势。',
              'No papers have been updated today. Incremental sync started and will generate the digest and trends when complete.',
            ),
        'ok',
      );
      void queryClient.invalidateQueries({ queryKey: ['ingest-state', libraryId] });
      void queryClient.invalidateQueries({ queryKey: ['library-digests', libraryId] });
      navigate(`/voyages/${result.voyage_id}`);
    },
    onError: (error) => {
      if (error instanceof ApiError && error.status === 409) {
        toast(
          tr(
            '当前已有文献任务运行中，完成后会自动生成简报。',
            'A literature task is already running; it will generate a digest when complete.',
          ),
          'info',
        );
        void queryClient.invalidateQueries({ queryKey: ['ingest-state', libraryId] });
        return;
      }
      toast(
        `${tr('简报任务启动失败：', 'Failed to start digest task: ')}${error instanceof Error ? error.message : String(error)}`,
        'error',
      );
    },
  });

  const listQuery = useQuery({
    queryKey: ['library-digests', libraryId],
    queryFn: () => api.listLibraryDigests(libraryId, 60),
    retry: false,
  });

  useEffect(() => {
    const rows = listQuery.data ?? [];
    if (!rows.length) {
      setSelectedId(null);
      return;
    }
    if (!selectedId || !rows.some((row) => row.id === selectedId)) {
      setSelectedId(rows[0]?.id ?? null);
    }
  }, [listQuery.data, selectedId]);

  const detailQuery = useQuery({
    queryKey: ['library-digest', libraryId, selectedId],
    queryFn: () => api.getLibraryDigest(libraryId, selectedId ?? ''),
    enabled: !!selectedId,
    retry: false,
  });

  const detail = detailQuery.data;
  const counts = useMemo(() => normalizeCounts(detail?.counts), [detail?.counts]);
  const paperTitles = useMemo(
    () => new Map((detail?.paper_insights ?? []).map((paper) => [paper.paper_id, paper.title])),
    [detail?.paper_insights],
  );

  if (listQuery.isLoading) {
    return <div className="skel" style={{ flex: 1, margin: 16 }} />;
  }
  if (listQuery.isError) {
    return (
      <EmptyState
        icon="x"
        title={tr('每日简报加载失败', 'Failed to load daily digests')}
        desc={tr('请检查后端服务后重试。', 'Check the backend service and try again.')}
        action={
          <button className="btn btn-soft sm" onClick={() => void listQuery.refetch()}>
            {tr('重试', 'Retry')}
          </button>
        }
      />
    );
  }
  if (!listQuery.data?.length) {
    return (
      <EmptyState
        icon="file"
        title={tr('还没有每日简报', 'No daily digest yet')}
        desc={tr(
          '下一次建库或增量同步会生成简报，并在简报成功后才推进同步水位线。',
          'The next ingest will create one, and the watermark advances only after it succeeds.',
        )}
        action={
          canGenerate ? (
            <button
              type="button"
              className="btn btn-primary sm"
              disabled={ingestRunning || generateMutation.isPending || !hasWatermark}
              title={
                hasWatermark
                  ? tr(
                      '今日已有论文更新时直接生成，否则先执行增量同步',
                      'Generate directly from today’s updates, or sync first when there are none',
                    )
                  : tr('需先完成一次初始建库', 'Run the initial library build first')
              }
              onClick={() => generateMutation.mutate()}
            >
              <Icon name="refresh" size={13} />
              {tr('生成今日简报', "Generate today's digest")}
            </button>
          ) : undefined
        }
      />
    );
  }

  return (
    <div style={{ display: 'flex', flexDirection: compact ? 'column' : 'row', flex: 1, minHeight: 0 }}>
      <aside
        style={{
          width: compact ? '100%' : 250,
          maxHeight: compact ? 190 : undefined,
          flexShrink: 0,
          overflowY: 'auto',
          borderRight: compact ? 'none' : '0.5px solid var(--border-2)',
          borderBottom: compact ? '0.5px solid var(--border-2)' : 'none',
          padding: 10,
        }}
      >
        <div style={{ padding: '4px 7px 9px', fontSize: 11.5, fontWeight: 680, color: 'var(--text-3)' }}>
          {tr('简报历史', 'Digest history')}
        </div>
        {listQuery.data.map((row) => {
          const active = row.id === selectedId;
          return (
            <button
              key={row.id}
              type="button"
              onClick={() => { setSelectedId(row.id); setView('brief'); }}
              style={{
                width: '100%',
                border: 'none',
                borderRadius: 8,
                padding: '9px 10px',
                marginBottom: 3,
                textAlign: 'left',
                cursor: 'pointer',
                background: active ? 'var(--accent-soft)' : 'transparent',
                color: active ? 'var(--accent)' : 'var(--text)',
                fontFamily: 'var(--sans)',
              }}
            >
              <div className="row" style={{ justifyContent: 'space-between', gap: 8 }}>
                <span style={{ fontSize: 12.5, fontWeight: 660 }}>{displayDate(row.report_date)}</span>
                {row.source === 'obsidian' && (
                  <span style={{ fontSize: 10.5, color: 'var(--text-3)' }}>Obsidian</span>
                )}
              </div>
              <div style={{ marginTop: 4, fontSize: 11.5, color: 'var(--text-3)' }}>
                {tr(`收录 ${row.counts.kept ?? 0} · 排除 ${row.counts.excluded ?? 0}`, `Kept ${row.counts.kept ?? 0} · Excluded ${row.counts.excluded ?? 0}`)}
              </div>
            </button>
          );
        })}
      </aside>

      <section style={{ flex: 1, minWidth: 0, minHeight: 0, overflowY: 'auto' }}>
        {detailQuery.isLoading ? (
          <div className="skel" style={{ height: 360, margin: 16 }} />
        ) : detailQuery.isError || !detail ? (
          <EmptyState
            compact
            icon="x"
            title={tr('这份简报暂时打不开', 'Cannot open this digest')}
            action={
              <button className="btn btn-soft sm" onClick={() => void detailQuery.refetch()}>
                {tr('重试', 'Retry')}
              </button>
            }
          />
        ) : (
          <div style={{ padding: compact ? 16 : 22, maxWidth: 980, margin: '0 auto' }}>
            <div className="row" style={{ justifyContent: 'space-between', gap: 12, flexWrap: 'wrap' }}>
              <div>
                <div className="row gap8" style={{ fontSize: 16, fontWeight: 720 }}>
                  <Icon name={view === 'brief' ? 'file' : 'chart'} size={17} />
                  {displayDate(detail.report_date)}
                </div>
                <div style={{ marginTop: 4, fontSize: 11.5, color: 'var(--text-3)' }}>
                  {detail.source === 'obsidian'
                    ? tr('从 Obsidian 文献库导入', 'Imported from the Obsidian vault')
                    : tr('由文献同步任务生成', 'Generated by the literature ingest')}
                </div>
              </div>
              <div className="row gap8" style={{ flexWrap: 'wrap', justifyContent: 'flex-end' }}>
                {canGenerate && (
                  <button
                    type="button"
                    className="btn btn-primary sm"
                    disabled={ingestRunning || generateMutation.isPending || !hasWatermark}
                    title={
                      hasWatermark
                        ? tr(
                            '今日已有论文更新时直接生成，否则先执行增量同步',
                            'Generate directly from today’s updates, or sync first when there are none',
                          )
                        : tr('需先完成一次初始建库', 'Run the initial library build first')
                    }
                    onClick={() => generateMutation.mutate()}
                  >
                    <Icon
                      name="refresh"
                      size={13}
                      style={generateMutation.isPending ? { animation: 'spin 1s linear infinite' } : undefined}
                    />
                    {generateMutation.isPending
                      ? tr('启动中…', 'Starting…')
                      : ingestRunning
                        ? tr('文献任务进行中', 'Literature task running')
                        : tr('生成今日简报', 'Generate today’s digest')}
                  </button>
                )}
                <Segmented<DigestView>
                  options={[
                    { v: 'brief', label: tr('本次简报', 'Digest') },
                    { v: 'trends', label: tr('滚动趋势', 'Rolling trends') },
                  ]}
                  value={view}
                  onChange={setView}
                />
              </div>
            </div>

            {view === 'brief' ? (
              <>
                <div className="row" style={{ gap: 8, flexWrap: 'wrap', marginTop: 18 }}>
                  <CountCard label={tr('来源抓取', 'Fetched')} value={counts.source_fetched} />
                  <CountCard label={tr('预筛后', 'Prescreened')} value={counts.prescreened} />
                  <CountCard label={tr('新候选', 'Inserted')} value={counts.inserted} />
                  <CountCard label={tr('保留', 'Kept')} value={counts.kept} />
                  <CountCard label={tr('排除', 'Excluded')} value={counts.excluded} />
                  <CountCard label={tr('已解读', 'Compiled')} value={counts.compiled} />
                </div>
                {detail.source_diagnostics.status === 'warning' && (
                  <div
                    style={{
                      marginTop: 14,
                      padding: '10px 12px',
                      borderRadius: 8,
                      background: 'var(--warn-bg)',
                      color: 'var(--warn-tx)',
                      fontSize: 12.5,
                    }}
                  >
                    {(detail.source_diagnostics.messages ?? []).join(' ')}
                  </div>
                )}
                <Markdown
                  source={detail.content}
                  onWikiLink={onWikiLink}
                  renderPaperRef={(paperId) => (
                    <button
                      type="button"
                      className="btn btn-ghost sm"
                      style={{ padding: '2px 7px', verticalAlign: 'middle' }}
                      onClick={() => onOpenPaper(paperId)}
                    >
                      {paperTitles.get(paperId) ?? tr('打开论文', 'Open paper')}
                    </button>
                  )}
                  style={{ marginTop: 22 }}
                />
              </>
            ) : detail.trend_content ? (
              <Markdown source={detail.trend_content} onWikiLink={onWikiLink} style={{ marginTop: 20 }} />
            ) : (
              <EmptyState
                compact
                icon="chart"
                title={tr('这一天还没有趋势快照', 'No trend snapshot for this day')}
                desc={tr(
                  '历史导入只在最新一份简报保留当前趋势；新的同步会逐次生成趋势快照。',
                  'Historical imports keep the current trends on the latest digest; future ingests create snapshots each time.',
                )}
              />
            )}
          </div>
        )}
      </section>
    </div>
  );
}
