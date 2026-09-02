import { useEffect, useMemo, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { ConfirmModal } from '../../components/ui/ConfirmModal';
import { FormField } from '../../components/ui/FormField';
import { Icon } from '../../components/ui/Icon';
import { Modal } from '../../components/ui/Modal';
import { Switch } from '../../components/ui/Switch';
import { toast } from '../../components/ui/Toast';
import {
  ApiError,
  api,
  type LiteratureProviderCredentialCreate,
  type LiteratureProviderCredentialUpdate,
  type LiteratureProviderHealth,
  type LiteratureProviderKeyStatus,
} from '../../lib/api';
import { tr } from '../../lib/i18n';
import {
  CREDENTIAL_SOURCES,
  SCORE_DIMENSIONS,
  SEARCH_SOURCES,
  buildLiteratureSettingsUpdate,
  draftFrom,
  sourceById,
  validateLiteratureSettingsDraft,
  type LiteratureSettingsDraft,
  type SourceDefinition,
} from './literatureSettingsModel';

function errorText(error: unknown): string {
  if (error instanceof ApiError) {
    const code = error.message.split(':')[0];
    if (code === 'INVALID_LITERATURE_SETTING') return tr('设置字段无效，请检查数值范围。', 'A setting is invalid; check the value range.');
    if (code === 'INVALID_LITERATURE_CREDENTIAL') return tr('密钥内容或来源无效。', 'The credential or provider is invalid.');
  }
  return error instanceof Error ? error.message : String(error);
}

function formatTime(timestamp: number | null | undefined): string {
  if (!timestamp) return tr('尚未测试', 'Not tested');
  return new Intl.DateTimeFormat(undefined, {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  }).format(new Date(timestamp * 1000));
}

function HealthBadge({ health }: { health: LiteratureProviderHealth | null | undefined }) {
  if (!health) return <span className="pill sm">{tr('未测试', 'Untested')}</span>;
  return (
    <span
      className="pill sm"
      title={`${health.detail} · ${formatTime(health.checked_at)}`}
      style={health.ok
        ? { background: 'var(--ok-bg)', color: 'var(--ok-tx)' }
        : { background: 'var(--danger-bg)', color: 'var(--danger-tx)' }}
    >
      <span className="dot" />
      {health.ok ? tr('可用', 'Available') : tr('失效', 'Failed')}
    </span>
  );
}

interface CredentialModalState {
  mode: 'create' | 'edit';
  source: string;
  credential?: LiteratureProviderKeyStatus;
}

interface CredentialDraft {
  label: string;
  secret: string;
  enabled: boolean;
}

const EMPTY_CREDENTIAL: CredentialDraft = { label: '', secret: '', enabled: true };

export function LiteratureSearchSettingsPanel() {
  const queryClient = useQueryClient();
  const settingsQuery = useQuery({
    queryKey: ['admin-literature-search-settings'],
    queryFn: () => api.getLiteratureSearchSettings(),
    retry: false,
  });
  const [draft, setDraft] = useState<LiteratureSettingsDraft | null>(null);
  const [badField, setBadField] = useState<string | null>(null);
  const [credentialModal, setCredentialModal] = useState<CredentialModalState | null>(null);
  const [credentialDraft, setCredentialDraft] = useState<CredentialDraft>(EMPTY_CREDENTIAL);
  const [deleteCredential, setDeleteCredential] = useState<LiteratureProviderKeyStatus | null>(null);

  useEffect(() => {
    if (settingsQuery.data && draft === null) setDraft(draftFrom(settingsQuery.data));
  }, [draft, settingsQuery.data]);

  const shown = draft ?? (settingsQuery.data ? draftFrom(settingsQuery.data) : null);
  const storedDraft = useMemo(
    () => (settingsQuery.data ? draftFrom(settingsQuery.data) : null),
    [settingsQuery.data],
  );
  const dirty = !!shown && !!storedDraft && JSON.stringify(shown) !== JSON.stringify(storedDraft);
  const credentials = settingsQuery.data?.provider_keys ?? {};
  const credentialSources = useMemo(() => {
    const known = new Set(CREDENTIAL_SOURCES.map((source) => source.id));
    const legacy = Object.keys(credentials)
      .filter((source) => source !== 'arxiv' && !known.has(source))
      .sort()
      .map(sourceById);
    return [...CREDENTIAL_SOURCES, ...legacy];
  }, [credentials]);

  const invalidate = () => queryClient.invalidateQueries({ queryKey: ['admin-literature-search-settings'] });

  const saveMutation = useMutation({
    mutationFn: (value: LiteratureSettingsDraft) => api.setLiteratureSearchSettings(buildLiteratureSettingsUpdate(value)),
    onSuccess: (saved) => {
      queryClient.setQueryData(['admin-literature-search-settings'], saved);
      setDraft(draftFrom(saved));
      setBadField(null);
      toast(tr('文献检索设置已保存', 'Literature search settings saved'), 'ok');
    },
    onError: (error) => toast(`${tr('保存失败', 'Save failed')}：${errorText(error)}`, 'error'),
  });

  const providerTestMutation = useMutation({
    mutationFn: (sourceId: string) => {
      const source = sourceById(sourceId);
      return api.testLiteratureProvider(sourceId, source.testQuery);
    },
    onSuccess: (result) => {
      void invalidate();
      toast(
        result.ok
          ? tr(`${sourceById(result.source).zh} 连接成功，${result.latency_ms} ms`, `${sourceById(result.source).en} connected in ${result.latency_ms} ms`)
          : tr(`${sourceById(result.source).zh} 连接失败：${result.detail}`, `${sourceById(result.source).en} failed: ${result.detail}`),
        result.ok ? 'ok' : 'error',
      );
    },
    onError: (error) => toast(`${tr('测试失败', 'Test failed')}：${errorText(error)}`, 'error'),
  });

  const createCredentialMutation = useMutation({
    mutationFn: (input: LiteratureProviderCredentialCreate) => api.createLiteratureProviderCredential(input),
    onSuccess: () => {
      setCredentialModal(null);
      void invalidate();
      toast(tr('密钥已加密保存', 'Credential saved encrypted'), 'ok');
    },
    onError: (error) => toast(`${tr('保存失败', 'Save failed')}：${errorText(error)}`, 'error'),
  });

  const updateCredentialMutation = useMutation({
    mutationFn: ({ id, input }: { id: string; input: LiteratureProviderCredentialUpdate }) => api.updateLiteratureProviderCredential(id, input),
    onSuccess: () => {
      setCredentialModal(null);
      void invalidate();
      toast(tr('密钥设置已更新', 'Credential updated'), 'ok');
    },
    onError: (error) => toast(`${tr('更新失败', 'Update failed')}：${errorText(error)}`, 'error'),
  });

  const toggleCredentialMutation = useMutation({
    mutationFn: (credential: LiteratureProviderKeyStatus) => api.updateLiteratureProviderCredential(credential.id, { enabled: !credential.enabled }),
    onSuccess: (credential) => {
      void invalidate();
      toast(credential.enabled ? tr('密钥已启用', 'Credential enabled') : tr('密钥已停用', 'Credential disabled'), 'ok');
    },
    onError: (error) => toast(`${tr('操作失败', 'Operation failed')}：${errorText(error)}`, 'error'),
  });

  const credentialTestMutation = useMutation({
    mutationFn: (credential: LiteratureProviderKeyStatus) => api.testLiteratureProviderCredential(credential.id, sourceById(credential.source).testQuery),
    onSuccess: (result) => {
      void invalidate();
      toast(
        result.ok ? tr(`密钥可用，${result.latency_ms} ms`, `Credential works, ${result.latency_ms} ms`) : tr(`密钥测试失败：${result.detail}`, `Credential test failed: ${result.detail}`),
        result.ok ? 'ok' : 'error',
      );
    },
    onError: (error) => toast(`${tr('测试失败', 'Test failed')}：${errorText(error)}`, 'error'),
  });

  const deleteCredentialMutation = useMutation({
    mutationFn: (id: string) => api.deleteLiteratureProviderCredential(id),
    onSuccess: () => {
      setDeleteCredential(null);
      void invalidate();
      toast(tr('密钥已删除', 'Credential deleted'), 'ok');
    },
    onError: (error) => toast(`${tr('删除失败', 'Delete failed')}：${errorText(error)}`, 'error'),
  });

  const openCreateCredential = (source: string) => {
    setCredentialDraft(EMPTY_CREDENTIAL);
    setCredentialModal({ mode: 'create', source });
  };

  const openEditCredential = (credential: LiteratureProviderKeyStatus) => {
    setCredentialDraft({ label: credential.label ?? '', secret: '', enabled: credential.enabled });
    setCredentialModal({ mode: 'edit', source: credential.source, credential });
  };

  const saveCredential = () => {
    if (!credentialModal) return;
    const label = credentialDraft.label.trim() || null;
    if (credentialModal.mode === 'create') {
      if (!credentialDraft.secret.trim()) return;
      createCredentialMutation.mutate({
        source: credentialModal.source,
        secret: credentialDraft.secret.trim(),
        label,
        enabled: credentialDraft.enabled,
      });
      return;
    }
    const input: LiteratureProviderCredentialUpdate = {
      label,
      enabled: credentialDraft.enabled,
    };
    if (credentialDraft.secret.trim()) input.secret = credentialDraft.secret.trim();
    updateCredentialMutation.mutate({ id: credentialModal.credential!.id, input });
  };

  const saveSettings = () => {
    if (!shown) return;
    const invalid = validateLiteratureSettingsDraft(shown);
    setBadField(invalid);
    if (invalid) {
      toast(tr('请先修正标出的检索设置', 'Fix the highlighted search settings first'), 'error');
      return;
    }
    saveMutation.mutate(shown);
  };

  if (settingsQuery.isLoading) return <div className="empty">{tr('加载中…', 'Loading…')}</div>;
  if (settingsQuery.isError || !shown) {
    return (
      <div className="card card-pad empty">
        {tr('无法加载文献检索设置（后端不可用或无权限）', 'Failed to load literature search settings (backend unavailable or no permission)')}
        <div style={{ marginTop: 10 }}>
          <button className="btn btn-soft sm" onClick={() => void settingsQuery.refetch()}>{tr('重试', 'Retry')}</button>
        </div>
      </div>
    );
  }

  const enabledSourceCount = shown.sources.length;
  const configuredKeyCount = Object.values(credentials).reduce((total, pool) => total + pool.length, 0);
  const healthyKeyCount = Object.values(credentials).flat().filter((credential) => credential.health?.ok).length;
  const credentialBusy = createCredentialMutation.isPending || updateCredentialMutation.isPending;

  return (
    <div className="literature-settings-stack">
      <div className="literature-settings-summary">
        <div>
          <div className="section-h">
            <Icon name="search" size={15} style={{ color: 'var(--accent)' }} />
            {tr('文献发现运行基线', 'Literature discovery baseline')}
          </div>
          <div className="literature-settings-intro">
            {tr(
              '这些是所有文献库新建检索的默认值。文献库自己的关键词、排除词和评分说明仍作为检索式生成与精排依据。',
              'These defaults seed every new library search. Each library’s keywords, exclusions, and scoring rubric still guide query generation and reranking.',
            )}
          </div>
        </div>
        <div className="literature-settings-stats" aria-label={tr('配置概览', 'Configuration summary')}>
          <div><strong>{enabledSourceCount}</strong><span>{tr('启用来源', 'sources')}</span></div>
          <div><strong>{configuredKeyCount}</strong><span>{tr('已存密钥', 'keys')}</span></div>
          <div><strong>{healthyKeyCount}</strong><span>{tr('测试可用', 'healthy')}</span></div>
        </div>
      </div>

      <div className="settings-main-side">
        <section className="card card-pad">
          <div className="section-h" style={{ marginBottom: 16 }}>
            <Icon name="sliders" size={15} style={{ color: 'var(--accent)' }} />
            {tr('检索运行', 'Retrieval run')}
          </div>
          <div className="settings-fields literature-settings-fields-compact">
            <FormField
              label={tr('返回数量', 'Result count')}
              hint={tr('每次精排后保留 1–200 篇。', 'Keep 1–200 papers after reranking.')}
              error={badField === 'requested_count' ? tr('请输入 1–200 的整数', 'Enter an integer from 1 to 200') : null}
            >
              <input className="input mono" type="number" min={1} max={200} value={shown.requested_count} onChange={(event) => setDraft({ ...shown, requested_count: Number(event.target.value) })} />
            </FormField>
            <FormField
              label={tr('候选预算', 'Candidate budget')}
              hint={tr('多源去重与复核前的候选上限。', 'Candidate cap before deduplication and review.')}
              error={badField === 'candidate_budget' || badField === 'candidate_budget_lt_requested' ? tr('应为 1–1000，且不小于返回数量', 'Use 1–1000 and not less than result count') : null}
            >
              <input className="input mono" type="number" min={1} max={1000} value={shown.candidate_budget} onChange={(event) => setDraft({ ...shown, candidate_budget: Number(event.target.value) })} />
            </FormField>
            <FormField
              label={tr('默认起始年份', 'Default start year')}
              hint={tr('留空表示不限。文献库检索时可覆盖。', 'Empty means no limit; library searches may override it.')}
              error={badField === 'start_year' || badField === 'year_window' ? tr('年份范围无效', 'Invalid year range') : null}
            >
              <input className="input mono" type="number" min={1800} max={3000} placeholder="2016" value={shown.start_year ?? ''} onChange={(event) => setDraft({ ...shown, start_year: event.target.value ? Number(event.target.value) : null })} />
            </FormField>
            <FormField
              label={tr('默认结束年份', 'Default end year')}
              hint={tr('留空表示截至当前。', 'Empty means through the present.')}
              error={badField === 'end_year' || badField === 'year_window' ? tr('年份范围无效', 'Invalid year range') : null}
            >
              <input className="input mono" type="number" min={1800} max={3000} placeholder={String(new Date().getFullYear())} value={shown.end_year ?? ''} onChange={(event) => setDraft({ ...shown, end_year: event.target.value ? Number(event.target.value) : null })} />
            </FormField>
          </div>
        </section>

        <section className="card card-pad">
          <div className="section-h" style={{ marginBottom: 6 }}>
            <Icon name="chart" size={15} style={{ color: 'var(--accent)' }} />
            {tr('评分标准', 'Scoring weights')}
          </div>
          <div className="literature-settings-intro" style={{ marginBottom: 12 }}>
            {tr('沿用 Polaris 五维评分基线；可按科研方向调整，系统会在排序时归一化。', 'Uses the Polaris five-dimension baseline; tune it by research direction and ranking will normalize it.')}
          </div>
          <div className="literature-weight-grid">
            {SCORE_DIMENSIONS.map((dimension) => (
              <label key={dimension.id} className="literature-weight-row">
                <span>{tr(dimension.zh, dimension.en)}</span>
                <input
                  className="input mono"
                  type="number"
                  min={0}
                  step={0.05}
                  value={shown.score_weights[dimension.id] ?? 0}
                  onChange={(event) => setDraft({
                    ...shown,
                    score_weights: { ...shown.score_weights, [dimension.id]: Number(event.target.value) },
                  })}
                />
              </label>
            ))}
          </div>
          {badField === 'score_weights' && <div className="field-error" style={{ marginTop: 8 }}>{tr('权重必须为非负数，且至少一项大于 0', 'Weights must be non-negative and at least one must be positive')}</div>}
        </section>
      </div>

      <section className="card card-pad">
        <div className="section-h" style={{ marginBottom: 6 }}>
          <Icon name="server" size={15} style={{ color: 'var(--accent)' }} />
          {tr('检索来源', 'Search sources')}
        </div>
        <div className="literature-settings-intro" style={{ marginBottom: 14 }}>
          {tr('勾选结果会实时成为新检索的来源快照。运行中的历史检索不受后续修改影响。', 'Enabled providers are snapshotted into new searches. Existing runs are unaffected by later changes.')}
        </div>
        {badField === 'sources' && <div className="field-error" style={{ marginBottom: 10 }}>{tr('至少启用一个检索来源', 'Enable at least one search source')}</div>}
        <div className="literature-source-grid">
          {SEARCH_SOURCES.map((source) => {
            const enabled = shown.sources.includes(source.id);
            const health = settingsQuery.data?.provider_health[source.id];
            const testing = providerTestMutation.isPending && providerTestMutation.variables === source.id;
            return (
              <div key={source.id} className={`literature-source-card${enabled ? ' enabled' : ''}`}>
                <div className="row" style={{ justifyContent: 'space-between', gap: 12 }}>
                  <div className="row gap8">
                    <strong>{source.zh}</strong>
                    {source.credentialMode === 'none' && <span className="pill sm">{tr('免密钥', 'No key')}</span>}
                    <HealthBadge health={health} />
                  </div>
                  <Switch
                    checked={enabled}
                    onChange={(checked) => {
                      setBadField(null);
                      setDraft({
                        ...shown,
                        sources: checked
                          ? SEARCH_SOURCES.filter((item) => item.id === source.id || shown.sources.includes(item.id)).map((item) => item.id)
                          : shown.sources.filter((id) => id !== source.id),
                      });
                    }}
                    aria-label={tr(`${checkedLabel(enabled)} ${source.zh}`, `${checkedLabelEn(enabled)} ${source.en}`)}
                  />
                </div>
                <p>{tr(source.descriptionZh, source.descriptionEn)}</p>
                <button className="btn btn-soft sm" disabled={providerTestMutation.isPending} onClick={() => providerTestMutation.mutate(source.id)}>
                  <Icon name={testing ? 'refresh' : 'play'} size={12} style={testing ? { animation: 'spin 1s linear infinite' } : undefined} />
                  {testing ? tr('测试中…', 'Testing…') : tr('测试来源', 'Test source')}
                </button>
              </div>
            );
          })}
        </div>
      </section>

      <section className="card card-pad">
        <div className="row literature-credentials-heading">
          <div>
            <div className="section-h">
              <Icon name="shield" size={15} style={{ color: 'var(--accent)' }} />
              {tr('来源凭据池', 'Provider credential pools')}
            </div>
            <div className="literature-settings-intro">
              {tr('密钥加密保存且永不回传明文。同一来源配置多个密钥时，后端按请求轮询。', 'Secrets are encrypted and never returned. Multiple credentials for one provider rotate across requests.')}
            </div>
          </div>
        </div>
        <div className="literature-credential-groups">
          {credentialSources.map((source) => (
            <CredentialGroup
              key={source.id}
              source={source}
              credentials={credentials[source.id] ?? []}
              providerHealth={settingsQuery.data?.provider_health[source.id]}
              testingId={credentialTestMutation.isPending ? credentialTestMutation.variables?.id : null}
              togglingId={toggleCredentialMutation.isPending ? toggleCredentialMutation.variables?.id : null}
              onAdd={() => openCreateCredential(source.id)}
              onEdit={openEditCredential}
              onDelete={setDeleteCredential}
              onTest={(credential) => credentialTestMutation.mutate(credential)}
              onToggle={(credential) => toggleCredentialMutation.mutate(credential)}
              onTestProvider={() => providerTestMutation.mutate(source.id)}
              providerTesting={providerTestMutation.isPending && providerTestMutation.variables === source.id}
            />
          ))}
        </div>
      </section>

      <div className="literature-settings-savebar">
        <div>
          <strong>{dirty ? tr('有未保存的检索设置', 'Unsaved search settings') : tr('检索设置已同步', 'Search settings are in sync')}</strong>
          <span>{tr('凭据增删改会单独即时保存。', 'Credential changes save immediately.')}</span>
        </div>
        <button className="btn btn-primary" disabled={!dirty || saveMutation.isPending} onClick={saveSettings}>
          {saveMutation.isPending ? tr('保存中…', 'Saving…') : tr('保存检索设置', 'Save search settings')}
        </button>
      </div>

      <Modal
        open={credentialModal !== null}
        onClose={() => !credentialBusy && setCredentialModal(null)}
        title={credentialModal?.mode === 'edit' ? tr('编辑来源密钥', 'Edit provider credential') : tr('新增来源密钥', 'Add provider credential')}
        sub={credentialModal ? tr(`${sourceById(credentialModal.source).zh} · 密钥只写不读`, `${sourceById(credentialModal.source).en} · secrets are write-only`) : undefined}
        width={500}
        footer={
          <>
            <button className="btn btn-ghost" disabled={credentialBusy} onClick={() => setCredentialModal(null)}>{tr('取消', 'Cancel')}</button>
            <button className="btn btn-primary" disabled={credentialBusy || (credentialModal?.mode === 'create' && !credentialDraft.secret.trim())} onClick={saveCredential}>
              {credentialBusy ? tr('保存中…', 'Saving…') : tr('保存', 'Save')}
            </button>
          </>
        }
      >
        <FormField label={tr('密钥标签', 'Credential label')} hint={tr('用于区分同一来源的多个密钥，例如“实验室主账号”。', 'Distinguishes keys in the same pool, such as “Lab primary”.')}>
          <input className="input" maxLength={120} value={credentialDraft.label} placeholder={tr('可选', 'Optional')} onChange={(event) => setCredentialDraft({ ...credentialDraft, label: event.target.value })} />
        </FormField>
        <FormField
          label={tr(credentialModal?.mode === 'edit' ? '替换密钥' : 'API Key / Token', credentialModal?.mode === 'edit' ? 'Replacement secret' : 'API key / token')}
          hint={credentialModal?.mode === 'edit' ? tr('留空保留当前密钥；输入新值才会替换。', 'Leave empty to keep the current secret; enter a value only to replace it.') : tr('保存后只显示末四位，无法再次读取。', 'Only the last four characters remain visible after saving.')}
        >
          <input className="input mono" type="password" autoComplete="new-password" value={credentialDraft.secret} placeholder={credentialModal?.mode === 'edit' ? tr('留空则不修改', 'Leave empty to keep') : '••••••••'} onChange={(event) => setCredentialDraft({ ...credentialDraft, secret: event.target.value })} />
        </FormField>
        <div className="settings-row">
          <div className="settings-row-text">
            <div className="field-label">{tr('启用此密钥', 'Enable this credential')}</div>
            <div className="field-hint">{tr('停用后保留配置，但不会进入轮询池。', 'Disabled credentials remain stored but leave the rotation pool.')}</div>
          </div>
          <Switch checked={credentialDraft.enabled} onChange={(enabled) => setCredentialDraft({ ...credentialDraft, enabled })} aria-label={tr('启用此密钥', 'Enable this credential')} />
        </div>
      </Modal>

      <ConfirmModal
        open={deleteCredential !== null}
        onClose={() => setDeleteCredential(null)}
        title={tr('删除来源密钥', 'Delete provider credential')}
        message={deleteCredential ? tr(`确定永久删除 ${sourceById(deleteCredential.source).zh} 的“${deleteCredential.label ?? deleteCredential.preview}”吗？删除后无法恢复。`, `Permanently delete “${deleteCredential.label ?? deleteCredential.preview}” from ${sourceById(deleteCredential.source).en}? This cannot be undone.`) : ''}
        confirmText={tr('删除', 'Delete')}
        danger
        busy={deleteCredentialMutation.isPending}
        onConfirm={() => deleteCredential && deleteCredentialMutation.mutate(deleteCredential.id)}
      />
    </div>
  );
}

function checkedLabel(enabled: boolean): string {
  return enabled ? '停用' : '启用';
}

function checkedLabelEn(enabled: boolean): string {
  return enabled ? 'Disable' : 'Enable';
}

interface CredentialGroupProps {
  source: SourceDefinition;
  credentials: LiteratureProviderKeyStatus[];
  providerHealth: LiteratureProviderHealth | undefined;
  testingId: string | null | undefined;
  togglingId: string | null | undefined;
  providerTesting: boolean;
  onAdd: () => void;
  onEdit: (credential: LiteratureProviderKeyStatus) => void;
  onDelete: (credential: LiteratureProviderKeyStatus) => void;
  onTest: (credential: LiteratureProviderKeyStatus) => void;
  onToggle: (credential: LiteratureProviderKeyStatus) => void;
  onTestProvider: () => void;
}

function CredentialGroup({
  source,
  credentials,
  providerHealth,
  testingId,
  togglingId,
  providerTesting,
  onAdd,
  onEdit,
  onDelete,
  onTest,
  onToggle,
  onTestProvider,
}: CredentialGroupProps) {
  return (
    <div className="literature-credential-group">
      <div className="literature-credential-group-head">
        <div>
          <div className="row gap8">
            <strong>{source.zh}</strong>
            <span className="pill sm">{source.metricOnly ? tr('期刊指标', 'Metrics') : source.credentialMode === 'required' ? tr('需要密钥', 'Key required') : tr('可选密钥', 'Optional key')}</span>
            <HealthBadge health={providerHealth} />
          </div>
          <p>{tr(source.descriptionZh, source.descriptionEn)}</p>
        </div>
        <div className="row gap6 wrap">
          <button className="btn btn-soft sm" disabled={providerTesting} onClick={onTestProvider}>
            <Icon name={providerTesting ? 'refresh' : 'play'} size={12} style={providerTesting ? { animation: 'spin 1s linear infinite' } : undefined} />
            {providerTesting ? tr('测试中…', 'Testing…') : tr('测试来源', 'Test provider')}
          </button>
          <button className="btn btn-primary sm" onClick={onAdd}>
            <Icon name="plus" size={12} />
            {tr('添加密钥', 'Add key')}
          </button>
        </div>
      </div>
      {credentials.length === 0 ? (
        <div className="literature-credential-empty">
          {source.credentialMode === 'required'
            ? tr('尚未配置密钥；该来源无法通过管理员密钥池运行。', 'No credential configured; this provider cannot use the administrator key pool.')
            : tr('尚未配置密钥；当前使用匿名额度或服务器环境变量。', 'No credential configured; anonymous quota or server environment fallback is used.')}
        </div>
      ) : (
        <div className="literature-credential-list">
          {credentials.map((credential) => {
            const testing = testingId === credential.id;
            const toggling = togglingId === credential.id;
            return (
              <div key={credential.id} className="literature-credential-row">
                <div className="literature-credential-identity">
                  <div className="row gap8 wrap">
                    <strong>{credential.label || tr('未命名密钥', 'Unnamed credential')}</strong>
                    <code>{credential.preview}</code>
                    <HealthBadge health={credential.health} />
                    {!credential.enabled && <span className="pill sm">{tr('已停用', 'Disabled')}</span>}
                  </div>
                  <span>{credential.health ? `${credential.health.detail} · ${formatTime(credential.health.checked_at)}` : tr('保存后尚未做独立连接测试', 'Not independently tested since it was saved')}</span>
                </div>
                <div className="row gap6 literature-credential-actions">
                  <Switch checked={credential.enabled} disabled={toggling} onChange={() => onToggle(credential)} aria-label={tr(`切换 ${credential.label ?? credential.preview}`, `Toggle ${credential.label ?? credential.preview}`)} />
                  <button className="btn btn-soft sm" disabled={testing} onClick={() => onTest(credential)}>
                    <Icon name={testing ? 'refresh' : 'play'} size={12} style={testing ? { animation: 'spin 1s linear infinite' } : undefined} />
                    {testing ? tr('测试中…', 'Testing…') : tr('测试', 'Test')}
                  </button>
                  <button className="icon-btn" title={tr('编辑密钥', 'Edit credential')} onClick={() => onEdit(credential)}><Icon name="pen" size={13} /></button>
                  <button className="icon-btn" title={tr('删除密钥', 'Delete credential')} onClick={() => onDelete(credential)}><Icon name="trash" size={13} /></button>
                </div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
