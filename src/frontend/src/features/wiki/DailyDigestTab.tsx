import { useEffect, useMemo, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useNavigate } from 'react-router-dom';
import { EmptyState } from '../../components/ui/EmptyState';
import { Icon } from '../../components/ui/Icon';
import { Segmented } from '../../components/ui/Segmented';
import { SpeechPlayer } from '../../components/ui/SpeechPlayer';
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
              `正在用今天的 ${result.paper_count} 篇新论文生成简报`,
              `Generating the digest from today’s ${result.paper_count} new papers`,
            )
          : tr(
              '正在同步新论文，完成后生成简报',
              'Syncing new papers. The digest follows when it’s done.',
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
            '已有任务在运行，完成后会生成简报',
            'A task is already running. The digest follows when it’s done.',
          ),
          'info',
        );
        void queryClient.invalidateQueries({ queryKey: ['ingest-state', libraryId] });
        return;
      }
      toast(
        `${tr('无法生成简报：', 'Couldn’t generate the digest: ')}${error instanceof Error ? error.message : String(error)}`,
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
        title={tr('无法加载每日简报', 'Couldn’t load daily digests')}
        desc={tr('请确认本机引擎正在运行。', 'Check that the local engine is running.')}
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
          '每次同步新论文后会生成简报。',
          'A digest is created after each sync.',
        )}
        action={
          <button
            type="button"
            className="btn btn-primary sm"
            disabled={ingestRunning || generateMutation.isPending || !hasWatermark}
            title={
              hasWatermark
                ? tr(
                    '用今天的新论文生成，没有则先同步',
                    'Uses today’s new papers, syncing first if there are none',
                  )
                : tr('完成初始建库后可用', 'Available after the first build')
            }
            onClick={() => generateMutation.mutate()}
          >
            <Icon name="refresh" size={13} />
            {tr('生成今日简报', 'Generate today’s digest')}
          </button>
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
                {tr(`加入 ${row.counts.kept ?? 0} · 排除 ${row.counts.excluded ?? 0}`, `Added ${row.counts.kept ?? 0} · Excluded ${row.counts.excluded ?? 0}`)}
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
            title={tr('无法打开这份简报', 'Couldn’t open this digest')}
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
                    ? tr('从 Obsidian 导入', 'Imported from Obsidian')
                    : tr('同步后自动生成', 'Created after a sync')}
                </div>
              </div>
              <div className="row gap8" style={{ flexWrap: 'wrap', justifyContent: 'flex-end' }}>
                {(view === 'brief' ? detail.content : detail.trend_content) && (
                  <SpeechPlayer
                    compact={false}
                    context="digest"
                    text={view === 'brief' ? detail.content : detail.trend_content ?? ''}
                  />
                )}
                <button
                  type="button"
                  className="btn btn-primary sm"
                  disabled={ingestRunning || generateMutation.isPending || !hasWatermark}
                  title={
                    hasWatermark
                      ? tr(
                          '用今天的新论文生成，没有则先同步',
                          'Uses today’s new papers, syncing first if there are none',
                        )
                      : tr('完成初始建库后可用', 'Available after the first build')
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
                      ? tr('任务进行中', 'Task running')
                      : tr('生成今日简报', 'Generate today’s digest')}
                </button>
                <Segmented<DigestView>
                  options={[
                    { v: 'brief', label: tr('本次简报', 'Digest') },
                    { v: 'trends', label: tr('近期趋势', 'Recent trends') },
                  ]}
                  value={view}
                  onChange={setView}
                />
              </div>
            </div>

            {view === 'brief' ? (
              <>
                <div className="row" style={{ gap: 8, flexWrap: 'wrap', marginTop: 18 }}>
                  <CountCard label={tr('抓取', 'Fetched')} value={counts.source_fetched} />
                  <CountCard label={tr('初筛后', 'After screening')} value={counts.prescreened} />
                  <CountCard label={tr('新候选', 'New candidates')} value={counts.inserted} />
                  <CountCard label={tr('加入', 'Added')} value={counts.kept} />
                  <CountCard label={tr('排除', 'Excluded')} value={counts.excluded} />
                  <CountCard label={tr('已解读', 'Summarized')} value={counts.compiled} />
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
                  '之后每次同步都会生成趋势。',
                  'Each future sync records trends.',
                )}
              />
            )}
          </div>
        )}
      </section>
    </div>
  );
}
