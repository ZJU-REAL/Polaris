/* 设置页「插件」tab（#707 列表/启停/配置 + #711 市场分区）。

   数据全部走桌面宿主桥的 plugins.* IPC（lib/host.ts，#706）：列表、启停、
   配置校验/保存、整树导出导入、卸载。市场分区（浏览/安装）在
   PluginsMarketSection，用 Segmented 与已安装列表切换——两边信息密度都
   不低，纵向堆叠会把「已安装」挤到第二屏。这个 tab 只在 plugins.manage
   能力可用时出现（SettingsPage 里判），「桥不可用」分支只是兜底。 */
import { useRef, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { ConfirmModal } from '../../components/ui/ConfirmModal';
import { EmptyState } from '../../components/ui/EmptyState';
import { Modal } from '../../components/ui/Modal';
import { Segmented } from '../../components/ui/Segmented';
import { Switch } from '../../components/ui/Switch';
import { toast } from '../../components/ui/Toast';
import { tr } from '../../lib/i18n';
import { SettingsGroup, SettingsSection, SettingsStack, StatusDot } from './settingsUi';
import {
  disablePlugin,
  enablePlugin,
  exportPluginTree,
  hasHost,
  importPluginTree,
  listPlugins,
  uninstallMarketPlugin,
  updatePluginConfig,
  validatePluginConfig,
  type PluginEntryInfo,
  type PluginTreeExport,
} from '../../lib/host';
import { saveBlob } from '../wiki/shared';
import { PluginsMarketSection } from './PluginsMarketSection';
import { parseConfigDraft, parseTreeFile, pluginBadge, validationErrorLines } from './pluginsView';

function errMsg(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

export function PluginsSettings() {
  const queryClient = useQueryClient();
  const bridged = hasHost();
  const [view, setView] = useState<'installed' | 'market'>('installed');
  const { data, isLoading, isError, refetch } = useQuery({
    queryKey: ['plugins'],
    queryFn: () => listPlugins(),
    enabled: bridged,
    retry: false,
  });

  // —— 配置编辑弹窗 ——
  const [configFor, setConfigFor] = useState<PluginEntryInfo | null>(null);
  const [draft, setDraft] = useState('');
  // 统一的错误行：JSON 语法错、schema 校验错都落到这里，红字逐行渲染
  const [errorLines, setErrorLines] = useState<string[]>([]);

  // —— 树导入 ——
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [pendingImport, setPendingImport] = useState<PluginTreeExport | null>(null);
  const [exporting, setExporting] = useState(false);

  const invalidate = () => void queryClient.invalidateQueries({ queryKey: ['plugins'] });

  const toggleMutation = useMutation({
    mutationFn: (p: PluginEntryInfo) => (p.disabled ? enablePlugin(p.id) : disablePlugin(p.id)),
    onSuccess: (updated) => {
      // 返回值就是变更后的条目：先就地写回让开关立刻到位，再 invalidate 兜住
      // 主进程侧可能连带变化的其他条目（比如级联停用）
      if (updated) {
        queryClient.setQueryData<PluginEntryInfo[] | null>(['plugins'], (prev) =>
          prev ? prev.map((p) => (p.id === updated.id ? updated : p)) : prev,
        );
      }
      invalidate();
    },
    onError: (e) => {
      toast(`${tr('操作失败', 'Something went wrong')}：${errMsg(e)}`, 'error');
      invalidate(); // 启动失败时主进程已回滚，拉一次拿到真实状态（可能带 error）
    },
  });

  const validateMutation = useMutation({
    mutationFn: (input: { name: string; config: Record<string, unknown> }) =>
      validatePluginConfig(input.name, input.config),
    onSuccess: (result) => {
      if (!result) return;
      if (result.ok) {
        setErrorLines([]);
        toast(tr('配置无误', 'Config is valid'), 'ok');
      } else {
        setErrorLines(validationErrorLines(result.errors));
      }
    },
    onError: (e) => setErrorLines([errMsg(e)]),
  });

  const saveMutation = useMutation({
    mutationFn: (input: { id: string; config: Record<string, unknown> }) =>
      updatePluginConfig(input.id, input.config),
    onSuccess: (result) => {
      if (!result) return;
      if (result.ok) {
        toast(tr('已保存', 'Saved'), 'ok');
        setConfigFor(null);
        invalidate();
      } else {
        // 没过 schema：错误回显在弹窗里，不关窗，用户接着改
        setErrorLines(validationErrorLines(result.errors));
      }
    },
    onError: (e) => setErrorLines([errMsg(e)]),
  });

  const importMutation = useMutation({
    mutationFn: (tree: PluginTreeExport) => importPluginTree(tree),
    onSuccess: () => {
      toast(tr('已导入', 'Imported'), 'ok');
      setPendingImport(null);
      invalidate();
    },
    onError: (e) => {
      toast(`${tr('导入失败', 'Import failed')}：${errMsg(e)}`, 'error');
      setPendingImport(null);
      invalidate(); // 主进程失败时已用快照回滚，刷新拿到回滚后的状态
    },
  });

  const uninstallMutation = useMutation({
    // 传条目 id 而不是 name：市场装的插件条目 name 是 file:// 入口 URL，
    // 包名只有 kernel 的安装记录知道，那边按 id 双解析（resolveInstallName）
    mutationFn: (p: PluginEntryInfo) => uninstallMarketPlugin(p.id),
    onSuccess: (result) => {
      if (!result) return;
      if (result.ok) {
        toast(tr('已卸载', 'Uninstalled'), 'ok');
        invalidate();
      } else {
        // 拒卸原因是数据不是异常：仍启用 / 不是市场装的，原话如实展示
        toast(result.message, 'error');
        invalidate();
      }
    },
    onError: (e) => toast(`${tr('卸载失败', 'Couldn’t uninstall')}：${errMsg(e)}`, 'error'),
  });

  const openConfig = (p: PluginEntryInfo) => {
    setConfigFor(p);
    setDraft(JSON.stringify(p.config ?? {}, null, 2));
    setErrorLines([]);
  };

  /** 校验/保存共用的前置：先在前端把 JSON 语法兜住，别拿语法错去打 IPC。 */
  const parsedDraft = (): Record<string, unknown> | null => {
    const parsed = parseConfigDraft(draft);
    if (!parsed.ok) {
      setErrorLines([tr(parsed.errorZh, parsed.errorEn)]);
      return null;
    }
    return parsed.config;
  };

  const doExport = async () => {
    setExporting(true);
    try {
      const tree = await exportPluginTree();
      if (!tree) return;
      saveBlob(
        new Blob([JSON.stringify(tree, null, 2)], { type: 'application/json' }),
        'polaris-plugins-tree.json',
      );
    } catch (e) {
      toast(`${tr('导出失败', 'Export failed')}：${errMsg(e)}`, 'error');
    } finally {
      setExporting(false);
    }
  };

  const pickImportFile = async (file: File) => {
    let text: string;
    try {
      text = await file.text();
    } catch (e) {
      toast(`${tr('无法读取文件', 'Couldn’t read the file')}：${errMsg(e)}`, 'error');
      return;
    }
    const parsed = parseTreeFile(text);
    if (!parsed.ok) {
      toast(tr(parsed.errorZh, parsed.errorEn), 'error');
      return;
    }
    setPendingImport(parsed.tree); // 先确认再动手：导入是全量替换
  };

  const plugins = data ?? [];
  const modalBusy = validateMutation.isPending || saveMutation.isPending;

  return (
    <SettingsStack>
    <div className="row gap8" style={{ flexWrap: 'wrap', alignItems: 'center' }}>
          <Segmented
            options={[
              { v: 'installed', label: tr('已安装', 'Installed') },
              { v: 'market', label: tr('插件市场', 'Market') },
            ]}
            value={view}
            onChange={setView}
          />
          {view === 'installed' && (
            <>
              <span style={{ flex: 1 }} />
              <button className="btn btn-ghost sm" disabled={!bridged || exporting} onClick={() => void doExport()}>
                {exporting ? tr('正在导出…', 'Exporting…') : tr('导出配置', 'Export config')}
              </button>
              <button
                className="btn btn-ghost sm"
                disabled={!bridged || importMutation.isPending}
                onClick={() => fileInputRef.current?.click()}
              >
                {tr('导入配置', 'Import config')}
              </button>
            </>
          )}
          <input
            ref={fileInputRef}
            type="file"
            accept="application/json,.json"
            style={{ display: 'none' }}
            onChange={(e) => {
              const f = e.target.files?.[0];
              if (f) void pickImportFile(f);
              e.target.value = ''; // 允许连续选同一个文件
            }}
          />
    </div>
    <SettingsSection
      title={view === 'market' ? tr('插件市场', 'Market') : tr('已安装', 'Installed')}
      desc={view === 'market'
        ? tr('新装的插件默认关闭，确认后再启用。', 'New plugins start off. Turn them on when you’re ready.')
        : tr('配置修改立即生效，导出的配置可在另一台电脑上导入。', 'Changes apply right away. Exported config can be imported on another computer.')}
    >

      {!bridged ? (
        <EmptyState
          icon="layers"
          title={tr('插件只能在桌面版中管理', 'Plugins can only be managed in the desktop app')}
        />
      ) : view === 'market' ? (
        <PluginsMarketSection />
      ) : isLoading ? (
        <div className="empty" style={{ padding: 24 }}>{tr('加载中…', 'Loading…')}</div>
      ) : isError || data == null ? (
        <EmptyState
          icon="layers"
          title={tr('无法加载插件', 'Couldn’t load plugins')}
          action={
            <button className="btn btn-soft sm" onClick={() => void refetch()}>{tr('重试', 'Retry')}</button>
          }
        />
      ) : plugins.length === 0 ? (
        <EmptyState
          icon="layers"
          title={tr('还没有安装插件', 'No plugins installed yet')}
          action={<button className="btn btn-soft sm" onClick={() => setView('market')}>{tr('浏览插件市场', 'Browse the market')}</button>}
        />
      ) : (
        <SettingsGroup>
          {plugins.map((p) => {
            const badge = pluginBadge(p);
            const busy = toggleMutation.isPending && toggleMutation.variables?.id === p.id;
            return (
              <div key={p.id} className="st-row">
                <div className="st-row-text">
                  <div className="row gap8" style={{ alignItems: 'center' }}>
                    <span className="st-row-label">{p.name}</span>
                    <StatusDot tone={p.state === 'active' ? 'ok' : p.state === 'error' ? 'err' : p.state === 'pending' ? 'warn' : 'idle'} title={badge.title}>
                      {tr(badge.zh, badge.en)}
                    </StatusDot>
                  </div>
                  {p.state === 'error' && p.error && (
                    <div style={{ fontSize: 11.5, color: 'var(--danger-tx)', marginTop: 3, lineHeight: 1.5 }}>
                      {p.error}
                    </div>
                  )}
                </div>
                <div className="row gap10" style={{ alignItems: 'center', flexShrink: 0 }}>
                  <button className="btn btn-ghost sm" onClick={() => openConfig(p)}>
                    {tr('配置', 'Configure')}
                  </button>
                  {/* 卸载只对停用条目开放；启用中的条目按钮置灰、悬停说明原因，
                      主进程侧同样拒卸（plugin-enabled），这里只是把话提前说 */}
                  <button
                    className="btn btn-ghost sm st-quiet-danger"
                    disabled={p.disabled ? uninstallMutation.isPending : true}
                    title={p.disabled ? undefined : tr('请先停用再卸载', 'Turn it off before uninstalling')}
                    onClick={() => {
                      if (p.disabled) uninstallMutation.mutate(p);
                    }}
                  >
                    {tr('卸载', 'Uninstall')}
                  </button>
                  <Switch
                    checked={!p.disabled}
                    disabled={busy}
                    onChange={() => toggleMutation.mutate(p)}
                    aria-label={tr(`启用 ${p.name}`, `Turn on ${p.name}`)}
                  />
                </div>
              </div>
            );
          })}
        </SettingsGroup>
      )}
    </SettingsSection>

      {/* —— 配置编辑弹窗：JSON 文本域 + 校验 + 保存 —— */}
      <Modal
        open={configFor !== null}
        onClose={() => setConfigFor(null)}
        width={560}
        title={configFor ? tr(`配置「${configFor.name}」`, `Configure “${configFor.name}”`) : ''}
        footer={
          <>
            <button className="btn btn-ghost" onClick={() => setConfigFor(null)}>{tr('取消', 'Cancel')}</button>
            <button
              className="btn btn-soft"
              disabled={modalBusy}
              onClick={() => {
                const config = parsedDraft();
                if (config && configFor) validateMutation.mutate({ name: configFor.name, config });
              }}
            >
              {validateMutation.isPending ? tr('检查中…', 'Checking…') : tr('检查', 'Check')}
            </button>
            <button
              className="btn btn-primary"
              disabled={modalBusy}
              onClick={() => {
                const config = parsedDraft();
                if (config && configFor) saveMutation.mutate({ id: configFor.id, config });
              }}
            >
              {saveMutation.isPending ? tr('保存中…', 'Saving…') : tr('保存', 'Save')}
            </button>
          </>
        }
      >
        <div style={{ fontSize: 12, color: 'var(--text-3)', marginBottom: 8, lineHeight: 1.5 }}>
          {tr('JSON 格式，保存前会检查。', 'JSON. It’s checked before saving.')}
        </div>
        <textarea
          className="textarea mono"
          style={{ minHeight: 220, fontSize: 12, width: '100%' }}
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          spellCheck={false}
        />
        {errorLines.length > 0 && (
          <div style={{ marginTop: 10, fontSize: 12, color: 'var(--danger-tx)', lineHeight: 1.6 }}>
            {errorLines.map((line, i) => (
              <div key={i} className="mono">{line}</div>
            ))}
          </div>
        )}
      </Modal>

      {/* —— 导入确认：全量替换，先说清楚再动手 —— */}
      <ConfirmModal
        open={pendingImport !== null}
        onClose={() => setPendingImport(null)}
        title={tr('导入插件配置？', 'Import plugin config?')}
        message={tr(
          `将替换全部插件配置（文件中有 ${pendingImport?.entries.length ?? 0} 个插件），导入失败会自动恢复。`,
          `This replaces all plugin config (${pendingImport?.entries.length ?? 0} in the file). It’s restored automatically if the import fails.`,
        )}
        confirmText={tr('导入', 'Import')}
        danger
        busy={importMutation.isPending}
        onConfirm={() => {
          if (pendingImport) importMutation.mutate(pendingImport);
        }}
      />
    </SettingsStack>
  );
}
