/* 设置页插件 tab 的「市场」分区（#711，插件市场计划 PR-7）。

   数据面全走桌面宿主桥的 plugins.market.* IPC（lib/host.ts，#708/#710）：
   索引、安装（job 进度）、索引源读写。装/启分离：安装完成的插件是
   disabled 条目，卡片就地给「启用」按钮（plugins.enable，#706），
   中间态「已安装，尚未启用」必须让用户看见，不静默代启。 */
import { useEffect, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { EmptyState } from '../../components/ui/EmptyState';
import { Icon } from '../../components/ui/Icon';
import { Modal } from '../../components/ui/Modal';
import { toast } from '../../components/ui/Toast';
import { invokeJob } from '../../lib/host-jobs';
import {
  enablePlugin,
  fetchMarketIndex,
  getMarketEndpoint,
  listPlugins,
  setMarketEndpoint,
  type MarketIndexEntry,
} from '../../lib/host';
import { tr } from '../../lib/i18n';
import {
  installPhaseText,
  marketEntryState,
  marketKindLabel,
  marketPermissionLabels,
  marketTierBadge,
  type InstallProgress,
  type MarketEntryState,
} from './marketView';

function errMsg(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

/** 安装进度条：四阶段等分，文字直说当前在干什么。 */
function InstallProgressBar({ progress }: { progress: InstallProgress }) {
  const phase = installPhaseText(progress.phase);
  const pct = progress.total > 0 ? Math.round((progress.done / progress.total) * 100) : 0;
  return (
    <div style={{ minWidth: 140 }}>
      <div style={{ fontSize: 11, color: 'var(--text-3)', marginBottom: 4 }}>
        {tr(phase.zh, phase.en)}（{progress.done}/{progress.total}）
      </div>
      <div style={{ height: 4, borderRadius: 2, background: 'var(--surface-3)', overflow: 'hidden' }}>
        <div style={{ height: '100%', width: `${pct}%`, background: 'var(--accent)', transition: 'width .3s' }} />
      </div>
    </div>
  );
}

/** 卡片右侧的状态/动作区：态机四态各有其形，中间态给就地「启用」。 */
function EntryAction({
  state,
  onInstall,
  onEnable,
  enabling,
}: {
  state: MarketEntryState;
  onInstall: () => void;
  onEnable: (pluginId: string) => void;
  enabling: boolean;
}) {
  switch (state.kind) {
    case 'installing':
      return <InstallProgressBar progress={state.progress} />;
    case 'installed-disabled':
      return (
        <div className="row gap8" style={{ alignItems: 'center' }}>
          <span className="pill sm" style={{ background: 'var(--warn-bg)', color: 'var(--warn-tx)' }}>
            {tr('已安装，尚未启用', 'Installed, not enabled')}
          </span>
          <button
            className="btn btn-primary sm"
            disabled={enabling}
            onClick={(e) => {
              e.stopPropagation();
              onEnable(state.pluginId);
            }}
          >
            {enabling ? tr('启用中…', 'Enabling…') : tr('启用', 'Enable')}
          </button>
        </div>
      );
    case 'installed-enabled':
      return (
        <span className="pill sm" style={{ background: 'var(--ok-bg)', color: 'var(--ok-tx)' }}>
          {tr('已启用', 'Enabled')}
        </span>
      );
    case 'not-installed':
    default:
      return (
        <button
          className="btn btn-soft sm"
          onClick={(e) => {
            e.stopPropagation();
            onInstall();
          }}
        >
          <Icon name="download" size={12} />
          {tr('安装', 'Install')}
        </button>
      );
  }
}

function MarketCard({
  entry,
  state,
  enabling,
  onOpen,
  onInstall,
  onEnable,
}: {
  entry: MarketIndexEntry;
  state: MarketEntryState;
  enabling: boolean;
  onOpen: () => void;
  onInstall: () => void;
  onEnable: (pluginId: string) => void;
}) {
  const kind = marketKindLabel(entry.kind);
  const tier = marketTierBadge(entry.tier);
  return (
    // 卡片里嵌着「安装/启用」按钮，外层不能再是 button（HTML 不允许按钮套按钮），
    // 用 div+role 的可点击卡片，内层按钮 stopPropagation
    <div
      className="card"
      role="button"
      tabIndex={0}
      onClick={onOpen}
      onKeyDown={(e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          onOpen();
        }
      }}
      style={{ padding: '12px 14px', cursor: 'pointer' }}
    >
      <div className="row gap8" style={{ alignItems: 'center', flexWrap: 'wrap' }}>
        <span style={{ fontSize: 13, fontWeight: 650 }}>{entry.name}</span>
        <span className="pill sm" style={{ background: 'var(--accent-soft)', color: 'var(--accent-text)' }}>
          {tr(kind.zh, kind.en)}
        </span>
        <span className="pill sm" style={{ background: tier.bg, color: tier.tx }}>
          {tr(tier.zh, tier.en)}
        </span>
        <span className="pill sm" style={{ background: 'var(--surface-3)', color: 'var(--text-3)' }}>
          v{entry.version}
        </span>
        <div style={{ marginLeft: 'auto', flexShrink: 0 }} onClick={(e) => e.stopPropagation()}>
          <EntryAction state={state} onInstall={onInstall} onEnable={onEnable} enabling={enabling} />
        </div>
      </div>
      <div style={{ fontSize: 12, color: 'var(--text-2)', marginTop: 6, lineHeight: 1.5 }}>{entry.description}</div>
      <div style={{ fontSize: 11, color: 'var(--text-3)', marginTop: 6 }}>
        {tr('发布者', 'Publisher')}：{entry.publisher}
      </div>
    </div>
  );
}

/** 详情弹窗：全字段 + 权限如实展示（v1 只展示声明，未做强制限制——这句必须说）。 */
function MarketDetailModal({
  entry,
  state,
  enabling,
  onClose,
  onInstall,
  onEnable,
}: {
  entry: MarketIndexEntry;
  state: MarketEntryState;
  enabling: boolean;
  onClose: () => void;
  onInstall: () => void;
  onEnable: (pluginId: string) => void;
}) {
  const kind = marketKindLabel(entry.kind);
  const tier = marketTierBadge(entry.tier);
  const perms = marketPermissionLabels(entry.permissions);
  return (
    <Modal
      open
      onClose={onClose}
      width={560}
      title={entry.name}
      sub={`v${entry.version} · ${tr(kind.zh, kind.en)} · ${tr('发布者', 'publisher')} ${entry.publisher}`}
      footer={
        state.kind === 'installing' ? (
          <InstallProgressBar progress={state.progress} />
        ) : state.kind === 'installed-disabled' ? (
          <>
            <span className="pill sm" style={{ background: 'var(--warn-bg)', color: 'var(--warn-tx)' }}>
              {tr('已安装，尚未启用', 'Installed, not enabled')}
            </span>
            <button className="btn btn-primary" disabled={enabling} onClick={() => onEnable(state.pluginId)}>
              {enabling ? tr('启用中…', 'Enabling…') : tr('启用', 'Enable')}
            </button>
          </>
        ) : state.kind === 'installed-enabled' ? (
          <span className="pill sm" style={{ background: 'var(--ok-bg)', color: 'var(--ok-tx)' }}>
            {tr('已启用', 'Enabled')}
          </span>
        ) : (
          <button className="btn btn-primary" onClick={onInstall}>
            <Icon name="download" size={13} />
            {tr('安装', 'Install')}
          </button>
        )
      }
    >
      <p style={{ fontSize: 12.5, color: 'var(--text-2)', lineHeight: 1.6, marginBottom: 12 }}>{entry.description}</p>
      <div className="row gap8" style={{ alignItems: 'center', flexWrap: 'wrap', marginBottom: 12 }}>
        <span className="pill sm" style={{ background: tier.bg, color: tier.tx }}>
          {tr(`质量分级：${tier.zh}`, `Tier: ${tier.en}`)}
        </span>
        {entry.badges.map((b) => (
          <span key={b} className="pill sm" style={{ background: 'var(--surface-3)', color: 'var(--text-2)' }}>
            {b}
          </span>
        ))}
      </div>
      <div style={{ fontSize: 12, fontWeight: 600, marginBottom: 6 }}>
        <Icon name="shield" size={12} style={{ marginRight: 4, verticalAlign: -2 }} />
        {tr('声明的权限', 'Declared permissions')}
      </div>
      <div className="row gap8" style={{ alignItems: 'center', flexWrap: 'wrap' }}>
        {perms.map((p, i) => (
          <span key={i} className="pill sm" style={{ background: 'var(--surface-3)', color: 'var(--text-2)' }}>
            {tr(p.zh, p.en)}
          </span>
        ))}
      </div>
      <div style={{ fontSize: 11, color: 'var(--text-3)', marginTop: 6, lineHeight: 1.5 }}>
        {tr(
          '当前版本仅展示插件声明的权限，尚未做强制限制。',
          'For now these are declarations only — they are not yet enforced.',
        )}
      </div>
    </Modal>
  );
}

export function PluginsMarketSection() {
  const queryClient = useQueryClient();

  const endpointQuery = useQuery({
    queryKey: ['plugin-market-endpoint'],
    queryFn: () => getMarketEndpoint(),
    retry: false,
  });
  const indexQuery = useQuery({
    queryKey: ['plugin-market-index'],
    queryFn: () => fetchMarketIndex(),
    retry: false,
  });
  // 已装列表与「已安装」视图共用同一个 queryKey，装完 invalidate 两边同时更新
  const pluginsQuery = useQuery({
    queryKey: ['plugins'],
    queryFn: () => listPlugins(),
    retry: false,
  });

  // —— 索引源编辑：草稿跟随查询结果，但用户改过后不再覆盖 ——
  const [endpointDraft, setEndpointDraft] = useState('');
  const [draftTouched, setDraftTouched] = useState(false);
  useEffect(() => {
    if (!draftTouched && endpointQuery.data) setEndpointDraft(endpointQuery.data.endpoint);
  }, [draftTouched, endpointQuery.data]);

  const endpointMutation = useMutation({
    mutationFn: (endpoint: string) => setMarketEndpoint(endpoint),
    onSuccess: (result) => {
      if (result) queryClient.setQueryData(['plugin-market-endpoint'], result);
      setDraftTouched(false); // 让草稿重新跟随保存后的真实值（复位默认时尤其需要）
      toast(tr('索引源已更新，正在刷新列表', 'Source updated; refreshing list'), 'ok');
      void queryClient.invalidateQueries({ queryKey: ['plugin-market-index'] });
    },
    onError: (e) => toast(`${tr('保存索引源失败', 'Failed to save source')}：${errMsg(e)}`, 'error'),
  });

  const enableMutation = useMutation({
    mutationFn: (pluginId: string) => enablePlugin(pluginId),
    onSuccess: () => {
      toast(tr('插件已启用', 'Plugin enabled'), 'ok');
      void queryClient.invalidateQueries({ queryKey: ['plugins'] });
    },
    onError: (e) => {
      toast(`${tr('启用失败', 'Enable failed')}：${errMsg(e)}`, 'error');
      void queryClient.invalidateQueries({ queryKey: ['plugins'] });
    },
  });

  // —— 安装：invoke 返回 jobId，进度经 job.* 事件推（invokeJob 已按 jobId 过滤）——
  // 按插件名记进行中的进度；组件卸载不取消安装（几秒内的本地任务，跑完
  // invalidate 自然生效），回调里的 toast/invalidate 不依赖组件存活
  const [installing, setInstalling] = useState<Record<string, InstallProgress | undefined>>({});
  const clearInstalling = (name: string) =>
    setInstalling((prev) => {
      const next = { ...prev };
      delete next[name];
      return next;
    });

  const startInstall = (entry: MarketIndexEntry) => {
    if (installing[entry.name]) return; // 防连点：同名安装同时只跑一个
    setInstalling((prev) => ({ ...prev, [entry.name]: { phase: 'download', done: 0, total: 4 } }));
    invokeJob(
      'plugins.market.install',
      { name: entry.name, version: entry.version },
      {
        onProgress: (p) => setInstalling((prev) => ({ ...prev, [entry.name]: p })),
        onDone: () => {
          clearInstalling(entry.name);
          toast(tr(`${entry.name} 已安装，尚未启用`, `${entry.name} installed, not yet enabled`), 'ok');
          void queryClient.invalidateQueries({ queryKey: ['plugins'] });
        },
        onError: (code, message) => {
          clearInstalling(entry.name);
          // 错误码如实带上（integrity-mismatch / registry-http…），方便照着报问题
          toast(`${tr('安装失败', 'Install failed')}（${code}）：${message}`, 'error');
          void queryClient.invalidateQueries({ queryKey: ['plugins'] });
        },
      },
    );
  };

  const [openEntry, setOpenEntry] = useState<string | null>(null);

  const entries = indexQuery.data ?? [];
  const plugins = pluginsQuery.data ?? [];
  const stateOf = (name: string) => marketEntryState(name, plugins, installing);
  const enablingId = enableMutation.isPending ? enableMutation.variables : null;
  const opened = openEntry !== null ? entries.find((e) => e.name === openEntry) : undefined;
  const endpoint = endpointQuery.data;

  return (
    <div>
      {/* 两个市场互相指路（#741）：这里只管插件；AI 任务技能的市场在「技能」页 */}
      <div style={{ fontSize: 11.5, color: 'var(--text-3)', marginBottom: 10 }}>
        {tr('这里是插件市场；AI 任务技能有自己的市场，在「技能」页。', 'This is the plugin market; AI task skills have their own market on the Skills page.')}
      </div>
      {/* —— 索引源设置：小输入框 + 保存/恢复默认 + 刷新 —— */}
      <div className="row gap8" style={{ alignItems: 'center', flexWrap: 'wrap', marginBottom: 6 }}>
        <span style={{ fontSize: 12, color: 'var(--text-2)', flexShrink: 0 }}>{tr('索引源', 'Source')}</span>
        <input
          className="input"
          style={{ flex: '1 1 220px', minWidth: 180, fontSize: 12 }}
          value={endpointDraft}
          placeholder="https://…"
          onChange={(e) => {
            setEndpointDraft(e.target.value);
            setDraftTouched(true);
          }}
        />
        <button
          className="btn btn-soft sm"
          disabled={endpointMutation.isPending || endpointDraft.trim() === ''}
          onClick={() => endpointMutation.mutate(endpointDraft.trim())}
        >
          {endpointMutation.isPending ? tr('保存中…', 'Saving…') : tr('保存', 'Save')}
        </button>
        {/* 空串 = 主进程复位官方默认源，前端不需要知道默认值是什么 */}
        <button
          className="btn btn-ghost sm"
          disabled={endpointMutation.isPending || endpoint?.isDefault === true}
          onClick={() => endpointMutation.mutate('')}
        >
          {tr('恢复默认', 'Reset to default')}
        </button>
        <button
          className="btn btn-soft sm"
          disabled={indexQuery.isFetching}
          onClick={() => void indexQuery.refetch()}
        >
          <Icon name="refresh" size={12} />
          {indexQuery.isFetching ? tr('刷新中…', 'Refreshing…') : tr('刷新', 'Refresh')}
        </button>
      </div>
      <div style={{ fontSize: 11, color: 'var(--text-3)', marginBottom: 14 }}>
        {endpoint
          ? endpoint.isDefault
            ? tr('当前使用官方源。', 'Using the official source.')
            : tr('当前使用自定义源。', 'Using a custom source.')
          : null}
      </div>

      {indexQuery.isLoading ? (
        <div className="empty" style={{ padding: 24 }}>{tr('加载中…', 'Loading…')}</div>
      ) : indexQuery.isError || indexQuery.data == null ? (
        <EmptyState
          icon="grid"
          title={tr('市场列表加载失败', 'Failed to load the market')}
          desc={`${tr('索引源', 'Source')}：${endpoint?.endpoint ?? tr('（未知）', '(unknown)')} — ${errMsg(indexQuery.error ?? tr('没有返回数据', 'No data returned'))}`}
          action={
            <button className="btn btn-soft sm" onClick={() => void indexQuery.refetch()}>
              {tr('重试', 'Retry')}
            </button>
          }
        />
      ) : entries.length === 0 ? (
        <EmptyState
          icon="grid"
          title={tr('市场里还没有插件', 'No plugins in the market yet')}
          desc={tr('这个索引源目前是空的。', 'This source has nothing listed right now.')}
        />
      ) : (
        <div style={{ display: 'grid', gap: 10 }}>
          {entries.map((entry) => {
            const state = stateOf(entry.name);
            return (
              <MarketCard
                key={`${entry.name}@${entry.version}`}
                entry={entry}
                state={state}
                enabling={state.kind === 'installed-disabled' && enablingId === state.pluginId}
                onOpen={() => setOpenEntry(entry.name)}
                onInstall={() => startInstall(entry)}
                onEnable={(id) => enableMutation.mutate(id)}
              />
            );
          })}
        </div>
      )}

      {opened && (
        <MarketDetailModal
          entry={opened}
          state={stateOf(opened.name)}
          enabling={
            stateOf(opened.name).kind === 'installed-disabled' &&
            enablingId === (stateOf(opened.name) as { pluginId?: string }).pluginId
          }
          onClose={() => setOpenEntry(null)}
          onInstall={() => startInstall(opened)}
          onEnable={(id) => enableMutation.mutate(id)}
        />
      )}
    </div>
  );
}
