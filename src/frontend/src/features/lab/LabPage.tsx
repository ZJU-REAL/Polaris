import { Suspense, lazy, useCallback, useEffect, useMemo, useState } from 'react';
import { Link, useNavigate, useSearchParams } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { Avatar } from '../../components/ui/Avatar';
import { Icon, type IconName } from '../../components/ui/Icon';
import { PageHead } from '../../components/ui/PageHead';
import { toast } from '../../components/ui/Toast';
import { Segmented } from '../../components/ui/Segmented';
import { StatCard } from '../../components/ui/StatCard';
import { EmptyState } from '../../components/ui/EmptyState';
import { useProject } from '../../app/project';
import { api, isAdmin, type DirectionLibrarySummary, type LabStats, type VoyageRead } from '../../lib/api';
import { fmtFullTime, fmtRelative } from '../../lib/format';
import { tr } from '../../lib/i18n';
import { useLibraries, libraryPath } from '../libraries/hooks';

// 图谱体量大且不是首屏必需：按需加载（与文献库工作台同样处理）
const GraphTab = lazy(() => import('../wiki/GraphTab').then((m) => ({ default: m.GraphTab })));
import {
  FILTERS,
  KIND_META,
  LIBRARY_TASK_KINDS,
  TOPIC_TASK_KINDS,
  SkeletonRows,
  VoyageRow,
  matchFilter,
  type Filter,
} from '../voyages/VoyagesPage';

/* ============================================================
   /lab — 实验室工作台
   两个标签：
   - 概况：实验室级数据面板 —— 索引与内容统计（/lab/stats，后端聚合、跨库去重、
     按可见库收敛）、AI 用量与排行榜（/lab/usage 系列）、跨库概念图谱
     （/lab/graph）、文献库与每日新论文汇总
   - 任务：全部可见任务按归属分组（课题任务 / 文献库任务 / 其它），
     覆盖课题工作台看不到的课题外任务（VoyageRun.project_id 可为空）
   ============================================================ */

type LabTab = 'overview' | 'tasks';

/* ------------------------------------------------------------------ 概况 */

/** 库状态小标：待审批 / 已驳回；已激活不额外占位（默认态不加噪）。 */
function LibStatusPill({ status }: { status: DirectionLibrarySummary['status'] }) {
  if (status === 'active') return null;
  const cfg =
    status === 'pending'
      ? { zh: '待审批', en: 'Pending', bg: 'var(--warn-bg)', tx: 'var(--warn-tx)' }
      : { zh: '已驳回', en: 'Rejected', bg: 'var(--danger-bg)', tx: 'var(--danger-tx)' };
  return (
    <span className="pill sm" style={{ background: cfg.bg, color: cfg.tx, flexShrink: 0 }}>
      {tr(cfg.zh, cfg.en)}
    </span>
  );
}

/** 文献库汇总卡：公共/个人计数 + 每库一行（论文数 / 概念数 / 上次同步时间 / 状态）。 */
function LibrariesCard() {
  const navigate = useNavigate();
  const { data, isLoading, isError } = useLibraries();
  const libs = useMemo(() => {
    const list = data ?? [];
    // 公共库在前，其次论文多的在前
    return [...list].sort(
      (a, b) => Number(b.is_public) - Number(a.is_public) || b.paper_count - a.paper_count,
    );
  }, [data]);
  const publicCount = libs.filter((l) => l.is_public).length;
  const personalCount = libs.length - publicCount;
  // 这里不再报「共 N 篇论文」：各库论文数相加会把跨库的同一篇重复计数，
  // 去重后的总数在上面的指标卡里（走 /lab/stats）。

  return (
    <div className="card" style={{ overflow: 'hidden', marginBottom: 20 }}>
      <div className="row gap10" style={{ padding: '14px 18px', borderBottom: '0.5px solid var(--border)' }}>
        <Icon name="book" size={15} style={{ color: 'var(--text-2)', flexShrink: 0 }} />
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ fontSize: 13.5, fontWeight: 650, letterSpacing: '-0.01em' }}>
            {tr('文献库', 'Libraries')}
          </div>
          <div style={{ fontSize: 11.5, color: 'var(--text-3)', marginTop: 2 }}>
            {isLoading || isError
              ? tr('实验室共享的方向文献库', 'Shared direction libraries')
              : tr(
                  `公共库 ${publicCount} 个 · 个人库 ${personalCount} 个`,
                  `${publicCount} public · ${personalCount} personal`,
                )}
          </div>
        </div>
        <Link className="btn btn-soft sm" to="/libraries">
          {tr('全部文献库', 'All libraries')}
          <Icon name="chevron" size={12} />
        </Link>
      </div>

      {isLoading ? (
        <div className="col gap10" style={{ padding: '16px 18px' }}>
          {[0, 1, 2].map((i) => (
            <div key={i} className="skel" style={{ height: 14, width: `${70 - i * 12}%` }} />
          ))}
        </div>
      ) : isError ? (
        <EmptyState
          icon="x"
          title={tr('无法加载文献库', 'Failed to load libraries')}
          desc={tr('后端不可用，稍后重试。', 'Backend unavailable — try again later.')}
          compact
        />
      ) : libs.length === 0 ? (
        <EmptyState
          icon="book"
          title={tr('还没有文献库', 'No libraries yet')}
          compact
          action={
            <Link className="btn btn-primary sm" to="/libraries">
              {tr('去建库', 'Create one')}
            </Link>
          }
        />
      ) : (
        <div className="table-wrap">
          {libs.map((l, i) => (
            <div
              key={l.id}
              className="voyage-row"
              onClick={() => navigate(libraryPath(l.id))}
              style={{ borderTop: i > 0 ? '0.5px solid var(--border)' : 'none' }}
            >
              <span
                className="pill sm"
                style={{
                  background: l.is_public ? 'var(--ok-bg)' : 'var(--violet-bg)',
                  color: l.is_public ? 'var(--ok-tx)' : 'var(--violet-tx)',
                  flexShrink: 0,
                }}
              >
                {l.is_public ? tr('公共', 'Public') : tr('个人', 'Personal')}
              </span>
              <div style={{ flex: 1, minWidth: 0 }}>
                <div
                  title={l.name}
                  style={{ fontSize: 13.5, fontWeight: 600, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}
                >
                  {l.name}
                </div>
                <div className="row gap8" style={{ marginTop: 4 }}>
                  {l.owner_name && (
                    <span style={{ fontSize: 11, color: 'var(--text-3)' }}>{l.owner_name}</span>
                  )}
                  <span style={{ fontSize: 11, color: 'var(--text-3)' }} title={fmtFullTime(l.last_synced_at)}>
                    {l.last_synced_at
                      ? tr(`上次同步 ${fmtRelative(l.last_synced_at)}`, `synced ${fmtRelative(l.last_synced_at)}`)
                      : tr('还没同步过', 'never synced')}
                  </span>
                </div>
              </div>
              <span className="mono" style={{ fontSize: 11.5, color: 'var(--text-3)', width: 96, flexShrink: 0, textAlign: 'right' }}>
                {l.paper_count} {tr('篇', 'papers')}
              </span>
              <span className="mono" style={{ fontSize: 11.5, color: 'var(--text-3)', width: 96, flexShrink: 0, textAlign: 'right' }}>
                {l.concept_count} {tr('概念', 'concepts')}
              </span>
              <span style={{ width: 70, display: 'flex', justifyContent: 'flex-end', flexShrink: 0 }}>
                <LibStatusPill status={l.status} />
              </span>
              <Icon name="chevron" size={14} style={{ color: 'var(--text-4)', flexShrink: 0 }} />
            </div>
          ))}
        </div>
      )}
    </div>
  );
}


/** 每日新论文汇总卡：今日新增 / 近 7 天合计 + 最近 7 天迷你分布（单色柱，标签用文本色）。 */
function DailyCard() {
  const { data, isLoading, isError } = useQuery({
    // 与 /daily 页共享缓存
    queryKey: ['daily-days'],
    queryFn: () => api.listDailyDays(),
    retry: false,
    staleTime: 60_000,
  });

  const days = useMemo(() => [...(data ?? [])].sort((a, b) => a.date.localeCompare(b.date)).slice(-7), [data]);
  // 「最新一批」而非「今天」：arxiv 按 UTC 出批次，用本地日期匹配会在跨时区/周末显示 0
  const today = days.length > 0 ? days[days.length - 1]!.date : '';
  const todayCount = days.length > 0 ? days[days.length - 1]!.count : 0;
  const weekTotal = days.reduce((acc, d) => acc + d.count, 0);
  const max = Math.max(1, ...days.map((d) => d.count));

  return (
    <div className="card" style={{ overflow: 'hidden' }}>
      <div className="row gap10" style={{ padding: '14px 18px', borderBottom: '0.5px solid var(--border)' }}>
        <Icon name="heart" size={15} style={{ color: 'var(--text-2)', flexShrink: 0 }} />
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ fontSize: 13.5, fontWeight: 650, letterSpacing: '-0.01em' }}>
            {tr('每日新论文', 'Daily Papers')}
          </div>
          <div style={{ fontSize: 11.5, color: 'var(--text-3)', marginTop: 2 }}>
            {tr('arXiv 每日新提交，保留最近 7 天', 'New arXiv submissions, last 7 days kept')}
          </div>
        </div>
        <Link className="btn btn-soft sm" to="/daily">
          {tr('去浏览', 'Browse')}
          <Icon name="chevron" size={12} />
        </Link>
      </div>

      {isLoading ? (
        <div className="col gap10" style={{ padding: '18px' }}>
          <div className="skel" style={{ height: 14, width: '40%' }} />
          <div className="skel" style={{ height: 56, width: '100%' }} />
        </div>
      ) : isError ? (
        <EmptyState
          icon="x"
          title={tr('无法加载每日新论文', 'Failed to load daily papers')}
          desc={tr('后端不可用，稍后重试。', 'Backend unavailable — try again later.')}
          compact
        />
      ) : days.length === 0 ? (
        <EmptyState
          icon="heart"
          title={tr('还没有抓取到新论文', 'No new papers yet')}
          desc={tr('每日抓取任务跑过之后，这里会显示每天的新增量。', 'Counts show up here once the daily fetch has run.')}
          compact
        />
      ) : (
        <div style={{ padding: '16px 18px' }}>
          <div className="row gap20" style={{ marginBottom: 16, flexWrap: 'wrap' }}>
            <div>
              <div className="mono" style={{ fontSize: 24, fontWeight: 700, letterSpacing: '-0.02em' }}>{todayCount}</div>
              <div style={{ fontSize: 11.5, color: 'var(--text-3)', marginTop: 2 }}>{tr('最新一批', 'Latest batch')}</div>
            </div>
            <div>
              <div className="mono" style={{ fontSize: 24, fontWeight: 700, letterSpacing: '-0.02em' }}>{weekTotal}</div>
              <div style={{ fontSize: 11.5, color: 'var(--text-3)', marginTop: 2 }}>{tr('近 7 天合计', 'Last 7 days')}</div>
            </div>
          </div>

          {/*
            单序列迷你分布：柱子统一 accent 色（明度区分靠高度），标签用文本色。
            /daily 目前不认 ?date=，柱子统一进列表页（日期在那里再选）。
          */}
          <div className="row" style={{ gap: 8, alignItems: 'flex-end' }}>
            {days.map((d) => (
              <Link
                key={d.date}
                to="/daily"
                title={`${d.date} · ${d.count}`}
                style={{ flex: 1, minWidth: 0, textDecoration: 'none', display: 'block' }}
              >
                <div style={{ height: 52, display: 'flex', alignItems: 'flex-end' }}>
                  <div
                    style={{
                      width: '100%',
                      height: `${Math.max(3, Math.round((d.count / max) * 52))}px`,
                      borderRadius: '4px 4px 2px 2px',
                      background: d.date === today ? 'var(--accent)' : 'var(--accent-soft)',
                      border: d.date === today ? 'none' : '0.5px solid var(--border-2)',
                    }}
                  />
                </div>
                <div
                  className="mono"
                  style={{ fontSize: 10, color: 'var(--text-3)', textAlign: 'center', marginTop: 5 }}
                >
                  {d.count}
                </div>
                <div
                  className="mono"
                  style={{ fontSize: 9.5, color: 'var(--text-4)', textAlign: 'center', marginTop: 1 }}
                >
                  {d.date.slice(5)}
                </div>
              </Link>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

/* ---------- 面板通用零件 ---------- */

/** 面板小节外壳：图标 + 标题 + 说明 + 右侧操作，与文献库/每日新论文两张卡同款。 */
function PanelCard({
  icon,
  title,
  hint,
  action,
  children,
  style,
}: {
  icon: IconName;
  title: string;
  hint?: string;
  action?: React.ReactNode;
  children: React.ReactNode;
  style?: React.CSSProperties;
}) {
  return (
    <div className="card" style={{ overflow: 'hidden', marginBottom: 20, ...style }}>
      <div className="row gap10" style={{ padding: '14px 18px', borderBottom: '0.5px solid var(--border)' }}>
        <Icon name={icon} size={15} style={{ color: 'var(--text-2)', flexShrink: 0 }} />
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ fontSize: 13.5, fontWeight: 650, letterSpacing: '-0.01em' }}>{title}</div>
          {hint && <div style={{ fontSize: 11.5, color: 'var(--text-3)', marginTop: 2 }}>{hint}</div>}
        </div>
        {action}
      </div>
      {children}
    </div>
  );
}

/** 「已完成 / 总数」的一行进度条。 */
function MeterRow({ label, done, total, note }: { label: string; done: number; total: number; note?: string }) {
  const pct = total > 0 ? Math.min(100, Math.round((done / total) * 100)) : 0;
  return (
    <div>
      <div className="row" style={{ justifyContent: 'space-between', alignItems: 'baseline', gap: 10 }}>
        <span style={{ fontSize: 12.5, color: 'var(--text-2)' }}>{label}</span>
        <span className="mono" style={{ fontSize: 12 }}>
          {done.toLocaleString()}
          <span style={{ color: 'var(--text-4)' }}> / {total.toLocaleString()}</span>
        </span>
      </div>
      <div style={{ height: 6, borderRadius: 999, background: 'var(--surface-3)', overflow: 'hidden', marginTop: 6 }}>
        <div style={{ width: `${pct}%`, height: '100%', borderRadius: 999, background: 'var(--accent)' }} />
      </div>
      {note && <div style={{ fontSize: 11, color: 'var(--text-4)', marginTop: 4, lineHeight: 1.45 }}>{note}</div>}
    </div>
  );
}

/* ---------- 索引与内容统计 ---------- */

/** 编译 / 全文分段 / 向量三条索引进度，数字全部来自 /lab/stats（跨库去重、按可见库收敛）。 */
function IndexCard({ stats, isLoading, isError }: { stats?: LabStats; isLoading: boolean; isError: boolean }) {
  return (
    <PanelCard
      icon="layers"
      title={tr('索引进度', 'Index coverage')}
      hint={tr('决定文献对话和语义检索能不能用', 'Decides whether chat and semantic search work')}
    >
      {isLoading ? (
        <div className="col gap10" style={{ padding: '16px 18px' }}>
          {[0, 1, 2].map((i) => (
            <div key={i} className="skel" style={{ height: 20, width: `${90 - i * 10}%` }} />
          ))}
        </div>
      ) : isError || !stats ? (
        <EmptyState
          icon="x"
          title={tr('无法加载统计', 'Failed to load stats')}
          desc={tr('后端不可用，稍后重试。', 'Backend unavailable — try again later.')}
          compact
        />
      ) : (
        <div style={{ padding: '16px 18px' }}>
          <div
            style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(240px, 1fr))', gap: '16px 28px' }}
          >
            <MeterRow
              label={tr('已编译解读的论文', 'Papers with reading notes')}
              done={stats.papers.compiled}
              total={stats.papers.library_members_deduped}
            />
            <MeterRow
              label={tr('已切好全文分段的论文', 'Papers with full-text chunks')}
              done={stats.chunks.papers_with_chunks}
              total={stats.papers.library_members_deduped}
              note={tr(`共 ${stats.chunks.total_chunks.toLocaleString()} 个分段`, `${stats.chunks.total_chunks.toLocaleString()} chunks in total`)}
            />
            <MeterRow
              label={tr('已建向量的分段', 'Chunks embedded')}
              done={stats.chunks.chunks_with_embedding}
              total={stats.chunks.total_chunks}
              note={
                stats.chunks.vector_search_supported
                  ? tr('这些分段可以按语义检索', 'These chunks are searchable by meaning')
                  : tr(
                      '当前数据库不支持向量检索，建好的向量暂时只能存着，检索会退回关键词',
                      'This database can’t search vectors — embeddings are stored but search falls back to keywords',
                    )
              }
            />
            <MeterRow
              label={tr('已建向量的论文', 'Papers embedded')}
              done={stats.vectors.papers_with_embedding}
              total={stats.vectors.papers_total}
            />
          </div>
          <div
            className="row gap16"
            style={{ marginTop: 16, paddingTop: 14, borderTop: '0.5px solid var(--border)', flexWrap: 'wrap' }}
          >
            <span style={{ fontSize: 11.5, color: 'var(--text-3)' }}>
              {tr(
                `内容池共 ${stats.papers.pool_total.toLocaleString()} 篇（含每日新论文与个人收藏，不只文献库）`,
                `${stats.papers.pool_total.toLocaleString()} papers in the shared pool (includes daily papers and personal picks, not just libraries)`,
              )}
            </span>
            <span style={{ fontSize: 11.5, color: 'var(--text-3)' }}>
              {tr(
                `概念 ${stats.concepts.total.toLocaleString()} 个`,
                `${stats.concepts.total.toLocaleString()} concepts`,
              )}
            </span>
          </div>
        </div>
      )}
    </PanelCard>
  );
}

/* ---------- AI 用量与排行榜 ---------- */

type UsageWindow = '7' | '30' | '90';

/** 每天一根柱：下段 prompt、上段 completion（单色系明暗区分，不新造颜色）。 */
function UsageBars({ rows }: { rows: { date: string; prompt: number; completion: number }[] }) {
  const max = Math.max(1, ...rows.map((r) => r.prompt + r.completion));
  // 柱子多的时候（30/90 天）只在两端标日期，免得文字糊成一片
  const labelEvery = Math.max(1, Math.ceil(rows.length / 8));
  return (
    <div className="row" style={{ gap: rows.length > 40 ? 2 : 5, alignItems: 'flex-end' }}>
      {rows.map((r, i) => {
        const total = r.prompt + r.completion;
        const h = Math.max(2, Math.round((total / max) * 88));
        const promptH = total > 0 ? Math.round((r.prompt / total) * h) : h;
        return (
          <div key={r.date} style={{ flex: 1, minWidth: 0 }}>
            <div
              title={`${r.date} · ${total.toLocaleString()} tokens`}
              style={{ height: 88, display: 'flex', flexDirection: 'column', justifyContent: 'flex-end' }}
            >
              <div
                style={{
                  height: h - promptH,
                  background: 'var(--accent)',
                  borderRadius: '3px 3px 0 0',
                }}
              />
              <div
                style={{
                  height: promptH,
                  background: 'var(--accent-soft)',
                  borderRadius: h - promptH > 0 ? '0 0 2px 2px' : '3px 3px 2px 2px',
                }}
              />
            </div>
            {i % labelEvery === 0 && (
              <div className="mono" style={{ fontSize: 9, color: 'var(--text-4)', textAlign: 'center', marginTop: 4 }}>
                {r.date.slice(5)}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

/** 全实验室 AI 用量：合计 + 按天柱状 + 按环节分布。 */
function UsageCard({ days, onDays }: { days: UsageWindow; onDays: (v: UsageWindow) => void }) {
  const { data, isLoading, isError } = useQuery({
    queryKey: ['lab-usage', days],
    queryFn: () => api.getLabUsage(Number(days)),
    retry: false,
    staleTime: 60_000,
  });
  const rows = useMemo(() => data ?? [], [data]);

  const totals = rows.reduce(
    (acc, r) => ({
      prompt: acc.prompt + r.prompt_tokens,
      completion: acc.completion + r.completion_tokens,
      calls: acc.calls + r.calls,
    }),
    { prompt: 0, completion: 0, calls: 0 },
  );

  const byDay = useMemo(() => {
    const m = new Map<string, { date: string; prompt: number; completion: number }>();
    for (const r of rows) {
      const cur = m.get(r.date) ?? { date: r.date, prompt: 0, completion: 0 };
      cur.prompt += r.prompt_tokens;
      cur.completion += r.completion_tokens;
      m.set(r.date, cur);
    }
    return [...m.values()].sort((a, b) => a.date.localeCompare(b.date));
  }, [rows]);

  // 按环节（stage）分布：取消耗最多的前 6 个
  const byStage = useMemo(() => {
    const m = new Map<string, number>();
    for (const r of rows) m.set(r.stage, (m.get(r.stage) ?? 0) + r.prompt_tokens + r.completion_tokens);
    return [...m.entries()].sort((a, b) => b[1] - a[1]).slice(0, 6);
  }, [rows]);
  const stageMax = Math.max(1, ...byStage.map(([, v]) => v));

  return (
    <PanelCard
      icon="chart"
      title={tr('AI 用量', 'AI usage')}
      hint={tr('全实验室的 token 消耗', 'Token consumption across the lab')}
      action={
        <Segmented
          options={[
            { v: '7' as const, label: tr('7 天', '7d') },
            { v: '30' as const, label: tr('30 天', '30d') },
            { v: '90' as const, label: tr('90 天', '90d') },
          ]}
          value={days}
          onChange={onDays}
        />
      }
      style={{ marginBottom: 0, flex: '1 1 380px', minWidth: 0 }}
    >
      {isLoading ? (
        <div className="col gap10" style={{ padding: '18px' }}>
          <div className="skel" style={{ height: 14, width: '40%' }} />
          <div className="skel" style={{ height: 88, width: '100%' }} />
        </div>
      ) : isError ? (
        <EmptyState
          icon="x"
          title={tr('无法加载用量', 'Failed to load usage')}
          desc={tr('后端不可用，稍后重试。', 'Backend unavailable — try again later.')}
          compact
        />
      ) : byDay.length === 0 ? (
        <EmptyState
          icon="chart"
          title={tr('这段时间没有 AI 调用', 'No AI calls in this window')}
          desc={tr('跑一次建库或精读编译，用量就会出现在这里。', 'Run a library build or a compile — usage shows up here afterwards.')}
          compact
        />
      ) : (
        <div style={{ padding: '16px 18px' }}>
          <div className="row gap20" style={{ marginBottom: 14, flexWrap: 'wrap' }}>
            <div>
              <div className="mono" style={{ fontSize: 24, fontWeight: 700, letterSpacing: '-0.02em' }}>
                {(totals.prompt + totals.completion).toLocaleString()}
              </div>
              <div style={{ fontSize: 11.5, color: 'var(--text-3)', marginTop: 2 }}>{tr('token 合计', 'total tokens')}</div>
            </div>
            <div>
              <div className="mono" style={{ fontSize: 24, fontWeight: 700, letterSpacing: '-0.02em' }}>
                {totals.calls.toLocaleString()}
              </div>
              <div style={{ fontSize: 11.5, color: 'var(--text-3)', marginTop: 2 }}>{tr('调用次数', 'calls')}</div>
            </div>
          </div>

          <UsageBars rows={byDay} />

          <div className="row gap12" style={{ marginTop: 12, flexWrap: 'wrap' }}>
            <span className="row gap8" style={{ fontSize: 11, color: 'var(--text-3)' }}>
              <span style={{ width: 9, height: 9, borderRadius: 2, background: 'var(--accent-soft)' }} />
              {tr('输入', 'Input')} {totals.prompt.toLocaleString()}
            </span>
            <span className="row gap8" style={{ fontSize: 11, color: 'var(--text-3)' }}>
              <span style={{ width: 9, height: 9, borderRadius: 2, background: 'var(--accent)' }} />
              {tr('输出', 'Output')} {totals.completion.toLocaleString()}
            </span>
          </div>

          {byStage.length > 0 && (
            <div style={{ marginTop: 16, paddingTop: 14, borderTop: '0.5px solid var(--border)' }}>
              <div style={{ fontSize: 11.5, color: 'var(--text-3)', marginBottom: 8 }}>
                {tr('消耗最多的环节', 'Where it goes')}
              </div>
              <div className="col gap8">
                {byStage.map(([stage, value]) => (
                  <div key={stage} className="row gap10">
                    <span className="mono" style={{ fontSize: 11.5, width: 108, flexShrink: 0, color: 'var(--text-2)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                      {stage}
                    </span>
                    <span style={{ flex: 1, minWidth: 0, height: 6, borderRadius: 999, background: 'var(--surface-3)', overflow: 'hidden' }}>
                      <span
                        style={{ display: 'block', width: `${Math.round((value / stageMax) * 100)}%`, height: '100%', borderRadius: 999, background: 'var(--accent)' }}
                      />
                    </span>
                    <span className="mono" style={{ fontSize: 11.5, color: 'var(--text-3)', width: 78, textAlign: 'right', flexShrink: 0 }}>
                      {value.toLocaleString()}
                    </span>
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
      )}
    </PanelCard>
  );
}

/** 成员用量排行榜：名次 + 头像 + 名称 + 用量条 + 数值。 */
function LeaderboardCard({ days, adminView }: { days: UsageWindow; adminView: boolean }) {
  const { data, isLoading, isError } = useQuery({
    queryKey: ['lab-leaderboard', days],
    queryFn: () => api.getLabLeaderboard({ days: Number(days), limit: 10 }),
    retry: false,
    staleTime: 60_000,
  });
  const items = data?.items ?? [];
  const max = Math.max(1, ...items.map((x) => x.tokens_used));

  return (
    <PanelCard
      icon="users"
      title={tr('用量排行榜', 'Usage leaderboard')}
      hint={tr(`最近 ${days} 天消耗最多的成员`, `Top consumers in the last ${days} days`)}
      action={
        adminView && data && !data.enabled ? (
          <span className="pill sm" style={{ background: 'var(--warn-bg)', color: 'var(--warn-tx)', flexShrink: 0 }}>
            {tr('仅管理员可见', 'Admins only')}
          </span>
        ) : undefined
      }
      style={{ marginBottom: 0, flex: '1 1 340px', minWidth: 0 }}
    >
      {isLoading ? (
        <div className="col gap10" style={{ padding: '16px 18px' }}>
          {[0, 1, 2].map((i) => (
            <div key={i} className="skel" style={{ height: 26, width: `${95 - i * 8}%` }} />
          ))}
        </div>
      ) : isError ? (
        <EmptyState
          icon="x"
          title={tr('无法加载排行榜', 'Failed to load leaderboard')}
          desc={tr('后端不可用，稍后重试。', 'Backend unavailable — try again later.')}
          compact
        />
      ) : items.length === 0 ? (
        <EmptyState
          icon="users"
          title={tr('这段时间还没有人用过 AI', 'Nobody used AI in this window')}
          desc={tr('有人跑过任务之后，排名会出现在这里。', 'Rankings show up once someone runs a task.')}
          compact
        />
      ) : (
        <div className="col gap10" style={{ padding: '14px 18px' }}>
          {items.map((u, i) => {
            const name = u.display_name || u.username || tr('未命名', 'Unnamed');
            const top = i === 0;
            return (
              <div key={u.user_id} className="row gap10">
                <span
                  className="mono"
                  style={{
                    width: 22,
                    height: 22,
                    flexShrink: 0,
                    borderRadius: 6,
                    fontSize: 11,
                    fontWeight: 700,
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'center',
                    background: top ? 'var(--accent-soft)' : 'var(--surface-3)',
                    color: top ? 'var(--accent-text)' : 'var(--text-3)',
                  }}
                >
                  {i + 1}
                </span>
                <Avatar userId={u.user_id} hasAvatar={u.has_avatar} name={name} size={24} />
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div
                    title={name}
                    style={{ fontSize: 12.5, fontWeight: 600, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}
                  >
                    {name}
                  </div>
                  <div style={{ height: 5, borderRadius: 999, background: 'var(--surface-3)', overflow: 'hidden', marginTop: 5 }}>
                    <div
                      style={{
                        width: `${Math.max(2, Math.round((u.tokens_used / max) * 100))}%`,
                        height: '100%',
                        borderRadius: 999,
                        background: top ? 'var(--accent)' : 'var(--accent-soft)',
                      }}
                    />
                  </div>
                </div>
                <span className="mono" style={{ fontSize: 11.5, color: 'var(--text-2)', width: 78, textAlign: 'right', flexShrink: 0 }}>
                  {u.tokens_used.toLocaleString()}
                </span>
              </div>
            );
          })}
        </div>
      )}
    </PanelCard>
  );
}

/* ---------- 跨库概念图谱 ---------- */

/** 跨库图谱：默认「趋势」视图＝概念随时间的演化；可切到单个文献库。 */
function GraphCard({ libs }: { libs: DirectionLibrarySummary[] }) {
  const navigate = useNavigate();
  const [libraryId, setLibraryId] = useState('');

  const openPaper = useCallback((id: string) => navigate(`/papers/${id}/read`), [navigate]);
  // 概念属于某个具体的库，先问后端它在哪个库，再跳到那个库的概念页
  const openConcept = useCallback(
    async (id: string) => {
      try {
        const concept = await api.getConcept(id);
        navigate(`${libraryPath(concept.library_id)}?conceptId=${id}`);
      } catch {
        toast(tr('打不开这个概念（后端不可用）', 'Could not open this concept (backend unavailable)'), 'error');
      }
    },
    [navigate],
  );

  return (
    <PanelCard
      icon="sparkle"
      title={tr('概念图谱', 'Concept graph')}
      action={
        <select
          className="input"
          aria-label={tr('选择文献库', 'Pick a library')}
          value={libraryId}
          onChange={(e) => setLibraryId(e.target.value)}
          style={{ height: 30, fontSize: 12, maxWidth: 200, padding: '0 8px', flexShrink: 0 }}
        >
          <option value="">{tr('全部文献库', 'All libraries')}</option>
          {libs.map((l) => (
            <option key={l.id} value={l.id}>
              {l.name}
            </option>
          ))}
        </select>
      }
    >
      <div style={{ display: 'flex', height: 560 }}>
        <Suspense fallback={<div className="skel" style={{ flex: 1, margin: 16 }} />}>
          <GraphTab
            scope="lab"
            libraryId={libraryId || undefined}
            onOpenPaper={openPaper}
            onOpenConcept={openConcept}
          />
        </Suspense>
      </div>
    </PanelCard>
  );
}

function OverviewTab() {
  const { data: libs } = useLibraries();
  const { data: me } = useQuery({ queryKey: ['me'], queryFn: () => api.me(), retry: false });
  // 库/论文/概念的数字一律走后端聚合：前端把各库 paper_count 相加会把
  // 跨库的同一篇论文重复计数，也拿不到分段与向量口径
  const statsQuery = useQuery({
    queryKey: ['lab-stats'],
    queryFn: () => api.getLabStats(),
    retry: false,
    staleTime: 60_000,
  });
  const { data: voyages } = useQuery({
    queryKey: ['voyages', 'all'],
    queryFn: () => api.listVoyages(),
    retry: false,
    refetchInterval: 30_000,
  });

  const [usageDays, setUsageDays] = useState<UsageWindow>('30');

  const stats = statsQuery.data;
  const libList = libs ?? [];
  const activeVoyages = (voyages ?? []).filter((v) => matchFilter(v, 'active') || matchFilter(v, 'paused')).length;
  const admin = isAdmin(me);
  // 开关关掉时排行榜只对管理员显示；加载中先不显示，免得闪一下又消失
  const showLeaderboard = !!stats && (stats.leaderboard_enabled || admin);

  return (
    <>
      <div className="row gap16 dash-stats" style={{ marginBottom: 20 }}>
        <StatCard
          icon="book"
          label="文献库"
          en="Libraries"
          value={stats ? stats.libraries.total : '—'}
          sub={stats ? tr(`${stats.libraries.public} 个公共库`, `${stats.libraries.public} public`) : undefined}
        />
        <StatCard
          icon="file"
          label="库内论文"
          en="Papers in libraries"
          value={stats ? stats.papers.library_members_deduped : '—'}
          sub={tr('跨库同一篇只算一次', 'deduplicated across libraries')}
        />
        <StatCard
          icon="sparkle"
          label="概念"
          en="Concepts"
          value={stats ? stats.concepts.total : '—'}
          sub={tr('全部文献库', 'across libraries')}
        />
        <StatCard
          icon="compass"
          label="进行中的任务"
          en="Running tasks"
          value={voyages ? activeVoyages : '—'}
          sub={tr('含等待审批', 'incl. waiting')}
          accent
        />
      </div>

      <IndexCard stats={stats} isLoading={statsQuery.isLoading} isError={statsQuery.isError} />

      <div className="row gap16" style={{ marginBottom: 20, alignItems: 'flex-start', flexWrap: 'wrap' }}>
        <UsageCard days={usageDays} onDays={setUsageDays} />
        {showLeaderboard && <LeaderboardCard days={usageDays} adminView={admin} />}
      </div>

      <GraphCard libs={libList} />

      <LibrariesCard />
      <DailyCard />
    </>
  );
}

/* ------------------------------------------------------------------ 任务 */

type GroupKey = string;

interface VoyageGroup {
  key: GroupKey;
  /** 分组标题（已本地化） */
  title: string;
  /** 分组说明（已本地化） */
  hint: string;
  icon: 'dashboard' | 'book' | 'sparkle';
  items: VoyageRead[];
}

/** 可折叠分组：标题行 + 计数 + 行列表。 */
function TaskGroup({ group, open, onToggle }: { group: VoyageGroup; open: boolean; onToggle: () => void }) {
  return (
    <div className="card" style={{ overflow: 'hidden', marginBottom: 14 }}>
      <button
        type="button"
        onClick={onToggle}
        aria-expanded={open}
        style={{
          width: '100%',
          display: 'flex',
          alignItems: 'center',
          gap: 10,
          padding: '12px 16px',
          background: 'transparent',
          border: 'none',
          cursor: 'pointer',
          textAlign: 'left',
          fontFamily: 'var(--sans)',
        }}
      >
        <Icon
          name="chevron"
          size={14}
          style={{ color: 'var(--text-3)', transform: open ? 'rotate(90deg)' : 'none', transition: 'transform .15s', flexShrink: 0 }}
        />
        <Icon name={group.icon} size={14} style={{ color: 'var(--text-3)', flexShrink: 0 }} />
        <span style={{ fontSize: 13, fontWeight: 650, color: 'var(--text)', letterSpacing: '-0.01em' }}>
          {group.title}
        </span>
        <span style={{ fontSize: 11.5, color: 'var(--text-4)', flex: 1, minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
          {group.hint}
        </span>
        <span className="pill sm" style={{ background: 'var(--surface-3)', color: 'var(--text-2)', flexShrink: 0 }}>
          {group.items.length}
        </span>
      </button>
      {open && (
        <div className="table-wrap" style={{ borderTop: '0.5px solid var(--border)' }}>
          {/* 归属名已在组标题上，行内不再重复 */}
          {group.items.map((v, i) => (
            <VoyageRow key={v.id} v={v} first={i === 0} />
          ))}
        </div>
      )}
    </div>
  );
}

function TasksTab() {
  const { projects } = useProject();
  const { data: libs } = useLibraries();

  const [filter, setFilter] = useState<Filter>('all');
  const [kindFilter, setKindFilter] = useState<string>('all');
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>({});

  // 不传 project_id = 我可见的全部任务（含 project_id 为空的课题外任务）
  const { data, isLoading, isError, refetch } = useQuery({
    queryKey: ['voyages', 'all'],
    queryFn: () => api.listVoyages(),
    retry: false,
    refetchInterval: 30_000,
  });

  const projectName = useMemo(() => new Map(projects.map((p) => [p.id, p.name] as const)), [projects]);
  const libraryName = useMemo(() => new Map((libs ?? []).map((l) => [l.id, l.name] as const)), [libs]);

  const groups = useMemo<VoyageGroup[]>(() => {
    const rows = (data ?? []).filter(
      (v) => matchFilter(v, filter) && (kindFilter === 'all' || v.kind === kindFilter),
    );
    const byProject = new Map<string, VoyageRead[]>();
    const byLibrary = new Map<string, VoyageRead[]>();
    const other: VoyageRead[] = [];
    for (const v of rows) {
      if (v.project_id) {
        const arr = byProject.get(v.project_id) ?? [];
        arr.push(v);
        byProject.set(v.project_id, arr);
      } else if (v.library_id) {
        const arr = byLibrary.get(v.library_id) ?? [];
        arr.push(v);
        byLibrary.set(v.library_id, arr);
      } else {
        other.push(v);
      }
    }
    const newest = (list: VoyageRead[]) =>
      [...list].sort((a, b) => b.created_at.localeCompare(a.created_at));
    const out: VoyageGroup[] = [];
    for (const [pid, list] of byProject) {
      out.push({
        key: `project:${pid}`,
        title: projectName.get(pid) ?? tr('未知课题', 'Unknown topic'),
        hint: tr('课题任务', 'Topic tasks'),
        icon: 'dashboard',
        items: newest(list),
      });
    }
    for (const [lid, list] of byLibrary) {
      out.push({
        key: `library:${lid}`,
        title: libraryName.get(lid) ?? tr('未知文献库', 'Unknown library'),
        hint: tr('文献库任务（课题外）', 'Library tasks (outside topics)'),
        icon: 'book',
        items: newest(list),
      });
    }
    if (other.length > 0) {
      out.push({
        key: 'other',
        title: tr('其它任务', 'Other tasks'),
        hint: tr('既不属于课题也不属于文献库', 'Neither topic nor library'),
        icon: 'sparkle',
        items: newest(other),
      });
    }
    // 课题组在前、文献库组次之、其它垫底；组内按任务数多的在前
    const rank = (g: VoyageGroup) => (g.key.startsWith('project:') ? 0 : g.key.startsWith('library:') ? 1 : 2);
    return out.sort((a, b) => rank(a) - rank(b) || b.items.length - a.items.length);
  }, [data, filter, kindFilter, projectName, libraryName]);

  return (
    <>
      <div className="row gap10" style={{ marginBottom: 16, flexWrap: 'wrap' }}>
        <Segmented options={FILTERS.map((f) => ({ v: f.v, label: tr(f.zh, f.en) }))} value={filter} onChange={setFilter} />
        <select
          className="input"
          aria-label={tr('按类型筛选', 'Filter by type')}
          value={kindFilter}
          onChange={(e) => setKindFilter(e.target.value)}
          style={{ height: 33, fontSize: 12.5, fontWeight: 600, width: 128, color: kindFilter === 'all' ? 'var(--text-3)' : 'var(--text)' }}
        >
          <option value="all">{tr('全部类型', 'All types')}</option>
          {/* 按任务层级分组：库任务与课题任务在实验室工作台都看得到，分开列
              比一长串平铺好找。以后每日新论文进任务系统，在这里加一组即可。 */}
          <optgroup label={tr('文献库任务', 'Library tasks')}>
            {LIBRARY_TASK_KINDS.map((k) => (
              <option key={k} value={k}>{tr(KIND_META[k]?.zh ?? k, KIND_META[k]?.en)}</option>
            ))}
          </optgroup>
          <optgroup label={tr('课题任务', 'Topic tasks')}>
            {TOPIC_TASK_KINDS.map((k) => (
              <option key={k} value={k}>{tr(KIND_META[k]?.zh ?? k, KIND_META[k]?.en)}</option>
            ))}
          </optgroup>
        </select>
      </div>

      {isLoading ? (
        <SkeletonRows />
      ) : isError ? (
        <div className="card">
          <EmptyState
            icon="x"
            title={tr('无法加载任务列表', 'Failed to load tasks')}
            desc={tr('后端不可用或接口尚未就绪，稍后可重试。', 'Backend unavailable or endpoint not ready — try again later.')}
            compact
            action={
              <button className="btn btn-soft" onClick={() => void refetch()}>
                <Icon name="refresh" size={13} />
                {tr('重试', 'Retry')}
              </button>
            }
          />
        </div>
      ) : groups.length === 0 ? (
        <div className="card">
          <EmptyState
            icon="compass"
            title={tr('暂无任务', 'No tasks yet')}
            desc={
              filter !== 'all' || kindFilter !== 'all'
                ? tr('当前筛选条件下没有任务，换个筛选试试。', 'No tasks match the current filters — try different ones.')
                : tr('发起建库、想法生成、实验等操作后，任务会出现在这里。', 'Tasks show up here once you start ingest, idea generation, experiments, and so on.')
            }
            compact
          />
        </div>
      ) : (
        groups.map((g) => (
          <TaskGroup
            key={g.key}
            group={g}
            open={!collapsed[g.key]}
            onToggle={() => setCollapsed((c) => ({ ...c, [g.key]: !c[g.key] }))}
          />
        ))
      )}
    </>
  );
}

/* ------------------------------------------------------------------ 页面 */

export function LabPage() {
  // ?tab= 深链进入后清参数（与课题工作台一致）
  const [tab, setTab] = useState<LabTab>('overview');
  const [searchParams, setSearchParams] = useSearchParams();
  useEffect(() => {
    const tabParam = searchParams.get('tab');
    if (!tabParam) return;
    if ((['overview', 'tasks'] as const).some((t) => t === tabParam)) setTab(tabParam as LabTab);
    setSearchParams({}, { replace: true });
  }, [searchParams, setSearchParams]);

  return (
    <div className="page fadeup">
      <PageHead
        eyebrow="Polaris · Lab"
        title={tr('实验室工作台', 'Lab Workbench')}
      />

      <div style={{ marginBottom: 22 }}>
        <Segmented
          options={[
            { v: 'overview' as const, label: tr('概况', 'Overview') },
            { v: 'tasks' as const, label: tr('任务', 'Tasks') },
          ]}
          value={tab}
          onChange={setTab}
        />
      </div>

      {tab === 'overview' ? <OverviewTab /> : <TasksTab />}
    </div>
  );
}
