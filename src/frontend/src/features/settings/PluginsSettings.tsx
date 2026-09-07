/* 设置页「插件」tab（#707，插件市场计划 PR-4 的前端面）。

   数据全部走桌面宿主桥的 plugins.* IPC（lib/host.ts，#706）：列表、启停、
   配置校验/保存、整树导出导入。这个 tab 只在 plugins.manage 能力可用时
   出现（SettingsPage 里判），所以这里的「桥不可用」分支只是兜底。 */
import { useRef, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { ConfirmModal } from '../../components/ui/ConfirmModal';
import { EmptyState } from '../../components/ui/EmptyState';
import { Icon } from '../../components/ui/Icon';
import { Modal } from '../../components/ui/Modal';
import { Switch } from '../../components/ui/Switch';
import { toast } from '../../components/ui/Toast';
import { tr } from '../../lib/i18n';
import {
  disablePlugin,
  enablePlugin,
  exportPluginTree,
  hasHost,
  importPluginTree,
  listPlugins,
  updatePluginConfig,
  validatePluginConfig,
  type PluginEntryInfo,
  type PluginTreeExport,
} from '../../lib/host';
import { saveBlob } from '../wiki/shared';
import { parseConfigDraft, parseTreeFile, pluginBadge, validationErrorLines } from './pluginsView';

function errMsg(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

export function PluginsSettings() {
  const queryClient = useQueryClient();
  const bridged = hasHost();
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
      toast(`${tr('操作失败', 'Failed')}：${errMsg(e)}`, 'error');
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
        toast(tr('配置没问题', 'Config looks good'), 'ok');
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
        toast(tr('配置已保存', 'Config saved'), 'ok');
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
      toast(tr('插件配置已导入', 'Plugin config imported'), 'ok');
      setPendingImport(null);
      invalidate();
    },
    onError: (e) => {
      toast(`${tr('导入失败', 'Import failed')}：${errMsg(e)}`, 'error');
      setPendingImport(null);
      invalidate(); // 主进程失败时已用快照回滚，刷新拿到回滚后的状态
    },
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
      toast(`${tr('读不了这个文件', 'Could not read the file')}：${errMsg(e)}`, 'error');
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
    <div className="card card-pad">
      <div className="row" style={{ justifyContent: 'space-between', marginBottom: 6, flexWrap: 'wrap', gap: 8 }}>
        <span className="section-h">
          <Icon name="layers" size={15} style={{ color: 'var(--accent)' }} />
          {tr('插件', 'Plugins')}
        </span>
        <div className="row gap8">
          <button className="btn btn-soft sm" disabled={!bridged || exporting} onClick={() => void doExport()}>
            <Icon name="download" size={12} />
            {exporting ? tr('导出中…', 'Exporting…') : tr('导出全部配置', 'Export all config')}
          </button>
          <button
            className="btn btn-soft sm"
            disabled={!bridged || importMutation.isPending}
            onClick={() => fileInputRef.current?.click()}
          >
            <Icon name="file" size={12} />
            {tr('从文件导入', 'Import from file')}
          </button>
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
      </div>
      <div style={{ fontSize: 11.5, color: 'var(--text-3)', marginBottom: 14, lineHeight: 1.5 }}>
        {tr(
          '管理这台电脑上装的插件：随时停用或启用，改配置立即生效。导出的配置文件可以拿到另一台电脑上导入。',
          'Manage plugins installed on this machine: enable or disable them anytime; config changes apply immediately. Exported config files can be imported on another machine.',
        )}
      </div>

      {!bridged ? (
        <EmptyState
          icon="layers"
          title={tr('插件管理不可用', 'Plugin management unavailable')}
          desc={tr('这个功能只在桌面客户端里可用。', 'This feature is only available in the desktop app.')}
        />
      ) : isLoading ? (
        <div className="empty" style={{ padding: 24 }}>{tr('加载中…', 'Loading…')}</div>
      ) : isError || data == null ? (
        <EmptyState
          icon="layers"
          title={tr('插件列表加载失败', 'Failed to load plugins')}
          action={
            <button className="btn btn-soft sm" onClick={() => void refetch()}>{tr('重试', 'Retry')}</button>
          }
        />
      ) : plugins.length === 0 ? (
        <EmptyState
          icon="layers"
          title={tr('还没有装任何插件', 'No plugins installed yet')}
          desc={tr('装上插件后，可以在这里启停和改配置。', 'Once plugins are installed, you can manage them here.')}
        />
      ) : (
        <div className="settings-list">
          {plugins.map((p) => {
            const badge = pluginBadge(p);
            const busy = toggleMutation.isPending && toggleMutation.variables?.id === p.id;
            return (
              <div key={p.id} className="settings-row" style={{ gap: 12 }}>
                <div className="settings-row-text" style={{ minWidth: 0 }}>
                  <div className="row gap8" style={{ alignItems: 'center' }}>
                    <span style={{ fontSize: 13, fontWeight: 600 }}>{p.name}</span>
                    <span className="pill sm" style={{ background: badge.bg, color: badge.tx }} title={badge.title}>
                      {tr(badge.zh, badge.en)}
                    </span>
                  </div>
                  {p.state === 'error' && p.error && (
                    <div style={{ fontSize: 11.5, color: 'var(--danger-tx)', marginTop: 3, lineHeight: 1.5 }}>
                      {p.error}
                    </div>
                  )}
                </div>
                <div className="row gap10" style={{ alignItems: 'center', flexShrink: 0 }}>
                  <button className="btn btn-soft sm" onClick={() => openConfig(p)}>
                    <Icon name="sliders" size={12} />
                    {tr('配置', 'Configure')}
                  </button>
                  <Switch
                    checked={!p.disabled}
                    disabled={busy}
                    onChange={() => toggleMutation.mutate(p)}
                    aria-label={tr(`启停插件 ${p.name}`, `Toggle plugin ${p.name}`)}
                  />
                </div>
              </div>
            );
          })}
        </div>
      )}

      {/* —— 配置编辑弹窗：JSON 文本域 + 校验 + 保存 —— */}
      <Modal
        open={configFor !== null}
        onClose={() => setConfigFor(null)}
        width={560}
        title={configFor ? tr(`配置：${configFor.name}`, `Configure: ${configFor.name}`) : ''}
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
              {validateMutation.isPending ? tr('校验中…', 'Checking…') : tr('校验', 'Check')}
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
          {tr('配置是一段 JSON。保存前会先按插件的要求检查，不合规不会生效。', 'Config is a JSON object. It is checked against the plugin schema before it takes effect.')}
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
        title={tr('从文件导入插件配置', 'Import plugin config from file')}
        message={tr(
          `将替换全部插件配置（文件里有 ${pendingImport?.entries.length ?? 0} 个插件），当前配置会自动备份一份，导入失败会自动恢复。`,
          `This replaces all plugin config (${pendingImport?.entries.length ?? 0} plugin(s) in the file). Your current config is backed up automatically and restored if the import fails.`,
        )}
        confirmText={tr('替换并导入', 'Replace and import')}
        danger
        busy={importMutation.isPending}
        onConfirm={() => {
          if (pendingImport) importMutation.mutate(pendingImport);
        }}
      />
    </div>
  );
}
