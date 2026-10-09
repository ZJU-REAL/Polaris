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
import { SettingsGroup, SettingsRow, SettingsSection, SettingsStack, StatusDot } from './settingsUi';
import {
  CREDENTIAL_SOURCES,
  RESOLVER_SOURCES,
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
    if (code === 'INVALID_LITERATURE_SETTING') return tr('有设置超出范围，请检查后重试', 'A value is out of range. Check it and try again.');
    if (code === 'INVALID_LITERATURE_CREDENTIAL') return tr('密钥无效', 'That key isn’t valid');
  }
  return error instanceof Error ? error.message : String(error);
}

function formatTime(timestamp: number | null | undefined): string {
  if (!timestamp) return tr('未测试', 'Not tested');
  return new Intl.DateTimeFormat(undefined, {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  }).format(new Date(timestamp * 1000));
}

function HealthBadge({ health }: { health: LiteratureProviderHealth | null | undefined }) {
  if (!health) return <StatusDot tone="idle">{tr('未测试', 'Not tested')}</StatusDot>;
  return (
    <StatusDot tone={health.ok ? 'ok' : 'err'} title={`${health.detail} · ${formatTime(health.checked_at)}`}>
      {health.ok ? tr('可用', 'Working') : tr('不可用', 'Not working')}
    </StatusDot>
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
      toast(tr('已保存', 'Saved'), 'ok');
    },
    onError: (error) => toast(`${tr('保存失败', 'Couldn’t save')}：${errorText(error)}`, 'error'),
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
          ? tr(`${sourceById(result.source).zh} 连接成功`, `${sourceById(result.source).en} connected`)
          : tr(`${sourceById(result.source).zh} 连接失败：${result.detail}`, `Couldn’t reach ${sourceById(result.source).en}: ${result.detail}`),
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
      toast(tr('已保存密钥', 'Key saved'), 'ok');
    },
    onError: (error) => toast(`${tr('保存失败', 'Couldn’t save')}：${errorText(error)}`, 'error'),
  });

  const updateCredentialMutation = useMutation({
    mutationFn: ({ id, input }: { id: string; input: LiteratureProviderCredentialUpdate }) => api.updateLiteratureProviderCredential(id, input),
    onSuccess: () => {
      setCredentialModal(null);
      void invalidate();
      toast(tr('已保存', 'Saved'), 'ok');
    },
    onError: (error) => toast(`${tr('保存失败', 'Couldn’t save')}：${errorText(error)}`, 'error'),
  });

  const toggleCredentialMutation = useMutation({
    mutationFn: (credential: LiteratureProviderKeyStatus) => api.updateLiteratureProviderCredential(credential.id, { enabled: !credential.enabled }),
    onSuccess: (credential) => {
      void invalidate();
      toast(credential.enabled ? tr('已启用', 'Turned on') : tr('已停用', 'Turned off'), 'ok');
    },
    onError: (error) => toast(`${tr('操作失败', 'Something went wrong')}：${errorText(error)}`, 'error'),
  });

  const credentialTestMutation = useMutation({
    mutationFn: (credential: LiteratureProviderKeyStatus) => api.testLiteratureProviderCredential(credential.id, sourceById(credential.source).testQuery),
    onSuccess: (result) => {
      void invalidate();
      toast(
        result.ok ? tr('密钥可用', 'Key works') : tr(`密钥不可用：${result.detail}`, `Key isn’t working: ${result.detail}`),
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
      toast(tr('已删除', 'Deleted'), 'ok');
    },
    onError: (error) => toast(`${tr('删除失败', 'Couldn’t delete')}：${errorText(error)}`, 'error'),
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
      toast(tr('请先修改标红的设置', 'Fix the highlighted settings first'), 'error');
      return;
    }
    saveMutation.mutate(shown);
  };

  if (settingsQuery.isLoading) return <div className="empty">{tr('加载中…', 'Loading…')}</div>;
  if (settingsQuery.isError || !shown) {
    return (
      <div className="card card-pad empty">
        {tr('无法加载文献检索设置', 'Couldn’t load literature search settings')}
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
    <SettingsStack>
      <SettingsSection
        title={tr('检索范围', 'Search scope')}
        desc={tr('文献库检索的默认设置，各文献库的关键词和评分说明仍然生效。', 'Defaults for library searches. Each library’s own keywords and scoring notes still apply.')}
      >
        <SettingsGroup>
          <SettingsRow
            label={tr('返回数量', 'Result count')}
            hint={tr('每次保留的论文数，1–200', 'Papers kept per search, 1–200')}
            error={badField === 'requested_count' ? tr('请输入 1–200 的整数', 'Enter a whole number from 1 to 200') : null}
          >
            <input className="input mono st-num" type="number" min={1} max={200} value={shown.requested_count} onChange={(event) => setDraft({ ...shown, requested_count: Number(event.target.value) })} />
          </SettingsRow>
          <SettingsRow
            label={tr('候选上限', 'Candidate limit')}
            hint={tr('筛选前最多收集的论文数，1–1000', 'Most papers collected before ranking, 1–1000')}
            error={badField === 'candidate_budget' || badField === 'candidate_budget_lt_requested' ? tr('须在 1–1000 之间，且不小于返回数量', 'Use 1–1000, at least the result count') : null}
          >
            <input className="input mono st-num" type="number" min={1} max={1000} value={shown.candidate_budget} onChange={(event) => setDraft({ ...shown, candidate_budget: Number(event.target.value) })} />
          </SettingsRow>
          <SettingsRow
            label={tr('年份范围', 'Years')}
            hint={tr('留空则不限', 'Leave empty for no limit')}
            error={badField === 'start_year' || badField === 'end_year' || badField === 'year_window' ? tr('年份范围无效', 'Invalid year range') : null}
          >
            <input className="input mono st-num" type="number" min={1800} max={3000} placeholder="2016" aria-label={tr('起始年份', 'From year')} value={shown.start_year ?? ''} onChange={(event) => setDraft({ ...shown, start_year: event.target.value ? Number(event.target.value) : null })} />
            <span style={{ color: 'var(--text-3)' }}>–</span>
            <input className="input mono st-num" type="number" min={1800} max={3000} placeholder={String(new Date().getFullYear())} aria-label={tr('结束年份', 'To year')} value={shown.end_year ?? ''} onChange={(event) => setDraft({ ...shown, end_year: event.target.value ? Number(event.target.value) : null })} />
          </SettingsRow>
        </SettingsGroup>
      </SettingsSection>

      <SettingsSection
        title={tr('排序权重', 'Ranking weights')}
        desc={badField === 'score_weights'
          ? <span style={{ color: 'var(--danger-tx)' }}>{tr('权重不能为负，且至少一项大于 0', 'Weights can’t be negative, and at least one must be above 0')}</span>
          : tr('数值越大，该项在排序中越重要。', 'Higher values count more in ranking.')}
      >
        <SettingsGroup>
          {SCORE_DIMENSIONS.map((dimension) => (
            <SettingsRow key={dimension.id} label={tr(dimension.zh, dimension.en)}>
              <input
                className="input mono st-num"
                type="number"
                min={0}
                step={0.05}
                aria-label={tr(dimension.zh, dimension.en)}
                value={shown.score_weights[dimension.id] ?? 0}
                onChange={(event) => setDraft({
                  ...shown,
                  score_weights: { ...shown.score_weights, [dimension.id]: Number(event.target.value) },
                })}
              />
            </SettingsRow>
          ))}
        </SettingsGroup>
      </SettingsSection>

      <SettingsSection
        title={tr('检索来源', 'Search sources')}
        desc={badField === 'sources'
          ? <span style={{ color: 'var(--danger-tx)' }}>{tr('请至少启用一个来源', 'Turn on at least one source')}</span>
          : tr(`已启用 ${enabledSourceCount} 个，只影响之后的检索。`, `${enabledSourceCount} on. Applies to new searches only.`)}
      >
        <SettingsGroup>
          {SEARCH_SOURCES.map((source) => {
            const enabled = shown.sources.includes(source.id);
            const health = settingsQuery.data?.provider_health[source.id];
            const testing = providerTestMutation.isPending && providerTestMutation.variables === source.id;
            return (
              <SettingsRow
                key={source.id}
                label={<span className="row gap8" style={{ alignItems: 'center' }}>{source.zh}<HealthBadge health={health} /></span>}
                hint={tr(source.descriptionZh, source.descriptionEn)}
              >
                <button className="btn btn-ghost sm" disabled={providerTestMutation.isPending} onClick={() => providerTestMutation.mutate(source.id)}>
                  {testing ? tr('测试中…', 'Testing…') : tr('测试', 'Test')}
                </button>
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
              </SettingsRow>
            );
          })}
        </SettingsGroup>
      </SettingsSection>

      <SettingsSection title={tr('免费全文', 'Free full text')} desc={tr('为找到的论文补充免费 PDF。', 'Adds free PDFs to papers found.')}>
        <SettingsGroup>
          {RESOLVER_SOURCES.map((source) => {
            const health = settingsQuery.data?.provider_health[source.id];
            const testing = providerTestMutation.isPending && providerTestMutation.variables === source.id;
            return (
              <SettingsRow
                key={source.id}
                label={<span className="row gap8" style={{ alignItems: 'center' }}>{source.zh}<HealthBadge health={health} /></span>}
                hint={tr(source.descriptionZh, source.descriptionEn)}
              >
                <button className="btn btn-ghost sm" disabled={providerTestMutation.isPending} onClick={() => providerTestMutation.mutate(source.id)}>
                  {testing ? tr('测试中…', 'Testing…') : tr('测试', 'Test')}
                </button>
              </SettingsRow>
            );
          })}
        </SettingsGroup>
      </SettingsSection>

      <SettingsSection
        title={tr('API 密钥', 'API keys')}
        desc={tr(`共 ${configuredKeyCount} 个，${healthyKeyCount} 个可用；同一来源有多个密钥时轮流使用，修改立即保存。`, `${configuredKeyCount} keys, ${healthyKeyCount} working. Several keys for one source are used in turn. Changes save right away.`)}
      >
        <SettingsGroup>
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
        </SettingsGroup>
      </SettingsSection>

      <div className="literature-settings-savebar">
        <div>
          <strong>{dirty ? tr('有未保存的修改', 'Unsaved changes') : tr('已保存', 'All changes saved')}</strong>
        </div>
        <button className="btn btn-primary" disabled={!dirty || saveMutation.isPending} onClick={saveSettings}>
          {saveMutation.isPending ? tr('保存中…', 'Saving…') : tr('保存', 'Save')}
        </button>
      </div>

      <Modal
        open={credentialModal !== null}
        onClose={() => !credentialBusy && setCredentialModal(null)}
        title={credentialModal?.mode === 'edit' ? tr('编辑密钥', 'Edit key') : tr('添加密钥', 'Add key')}
        sub={credentialModal ? sourceById(credentialModal.source).zh : undefined}
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
        <FormField label={tr('名称', 'Name')} hint={tr('用来区分同一来源的多个密钥', 'Tells keys for the same source apart')}>
          <input className="input" maxLength={120} value={credentialDraft.label} placeholder={tr('例如 备用', 'e.g. Backup')} onChange={(event) => setCredentialDraft({ ...credentialDraft, label: event.target.value })} />
        </FormField>
        <FormField
          label={tr(credentialModal?.mode === 'edit' ? '新密钥' : 'API Key', credentialModal?.mode === 'edit' ? 'New key' : 'API key')}
          hint={credentialModal?.mode === 'edit' ? tr('留空则不修改', 'Leave empty to keep the current key') : tr('保存后只显示末 4 位', 'Only the last 4 characters are shown after saving')}
        >
          <input className="input mono" type="password" autoComplete="new-password" value={credentialDraft.secret} placeholder={'••••••••'} onChange={(event) => setCredentialDraft({ ...credentialDraft, secret: event.target.value })} />
        </FormField>
        <div className="settings-row">
          <div className="settings-row-text">
            <div className="field-label">{tr('启用', 'On')}</div>
            <div className="field-hint">{tr('停用的密钥会保留，但不会被使用', 'Turned-off keys are kept but not used')}</div>
          </div>
          <Switch checked={credentialDraft.enabled} onChange={(enabled) => setCredentialDraft({ ...credentialDraft, enabled })} aria-label={tr('启用此密钥', 'Turn on this key')} />
        </div>
      </Modal>

      <ConfirmModal
        open={deleteCredential !== null}
        onClose={() => setDeleteCredential(null)}
        title={deleteCredential ? tr(`删除密钥「${deleteCredential.label ?? deleteCredential.preview}」？`, `Delete key “${deleteCredential.label ?? deleteCredential.preview}”?`) : ''}
        message={deleteCredential ? tr(`${sourceById(deleteCredential.source).zh} 将不再使用它，删除后无法恢复。`, `${sourceById(deleteCredential.source).en} stops using it. This can’t be undone.`) : ''}
        confirmText={tr('删除', 'Delete')}
        danger
        busy={deleteCredentialMutation.isPending}
        onConfirm={() => deleteCredential && deleteCredentialMutation.mutate(deleteCredential.id)}
      />
    </SettingsStack>
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
    <div className="st-row st-row-stack" style={{ gap: 6 }}>
      <div className="row gap8" style={{ alignItems: 'center', flexWrap: 'wrap' }}>
        <span className="st-row-label">{source.zh}</span>
        <span className="st-row-hint" style={{ marginTop: 0 }}>
          {source.metricOnly ? tr('期刊指标', 'Metrics') : source.credentialMode === 'required' ? tr('需要密钥', 'Key required') : tr('密钥可选', 'Key optional')}
        </span>
        <HealthBadge health={providerHealth} />
        <span style={{ flex: 1 }} />
        <button className="btn btn-ghost sm" disabled={providerTesting} onClick={onTestProvider}>
          {providerTesting ? tr('测试中…', 'Testing…') : tr('测试', 'Test')}
        </button>
        <button className="btn btn-ghost sm" onClick={onAdd}>
          <Icon name="plus" size={12} />
          {tr('添加密钥', 'Add key')}
        </button>
      </div>
      {credentials.length === 0 ? (
        <div className="st-row-hint">
          {source.credentialMode === 'required'
            ? tr('还没有密钥，添加后才能使用这个来源', 'No key yet. Add one to use this source.')
            : tr('还没有密钥，目前使用免费额度', 'No key yet. Using the free quota.')}
        </div>
      ) : (
        credentials.map((credential) => {
          const testing = testingId === credential.id;
          const toggling = togglingId === credential.id;
          return (
            <div key={credential.id} className="row gap8" style={{ alignItems: 'center', flexWrap: 'wrap', paddingLeft: 12 }}>
              <span style={{ fontSize: 13 }}>{credential.label || tr('未命名', 'Unnamed')}</span>
              <code className="mono" style={{ fontSize: 12, color: 'var(--text-3)' }}>{credential.preview}</code>
              {credential.enabled
                ? <HealthBadge health={credential.health} />
                : <StatusDot tone="idle">{tr('已停用', 'Off')}</StatusDot>}
              <span style={{ flex: 1 }} />
              <button className="btn btn-ghost sm" disabled={testing} onClick={() => onTest(credential)}>
                {testing ? tr('测试中…', 'Testing…') : tr('测试', 'Test')}
              </button>
              <button className="icon-btn" title={tr('编辑', 'Edit')} aria-label={tr('编辑', 'Edit')} onClick={() => onEdit(credential)}><Icon name="pen" size={13} /></button>
              <button className="icon-btn st-quiet-danger" title={tr('删除', 'Delete')} aria-label={tr('删除', 'Delete')} onClick={() => onDelete(credential)}><Icon name="trash" size={13} /></button>
              <Switch checked={credential.enabled} disabled={toggling} onChange={() => onToggle(credential)} aria-label={tr(`启用 ${credential.label ?? credential.preview}`, `Turn on ${credential.label ?? credential.preview}`)} />
            </div>
          );
        })
      )}
    </div>
  );
}
