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
  type DocumentProcessingCredentialCreate,
  type DocumentProcessingCredentialStatus,
  type DocumentProcessingCredentialUpdate,
  type LiteratureProviderHealth,
} from '../../lib/api';
import { tr } from '../../lib/i18n';
import {
  documentProcessingDraftFrom,
  validateDocumentProcessingDraft,
  type DocumentProcessingDraft,
} from './documentProcessingSettingsModel';

interface CredentialDraft {
  label: string;
  secret: string;
  enabled: boolean;
}

interface CredentialModalState {
  mode: 'create' | 'edit';
  credential?: DocumentProcessingCredentialStatus;
}

const EMPTY_CREDENTIAL: CredentialDraft = { label: '', secret: '', enabled: true };

function errorText(error: unknown): string {
  if (error instanceof ApiError) {
    const code = error.message.split(':')[0];
    if (code === 'INVALID_DOCUMENT_PROCESSING_SETTING') {
      return tr('文档处理设置无效，请检查标出的字段。', 'Document-processing settings are invalid.');
    }
    if (code === 'INVALID_DOCUMENT_PROCESSING_CREDENTIAL') {
      return tr('MinerU 密钥无效。', 'The MinerU credential is invalid.');
    }
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

export function DocumentProcessingSettingsPanel() {
  const queryClient = useQueryClient();
  const settingsQuery = useQuery({
    queryKey: ['admin-document-processing-settings'],
    queryFn: () => api.getDocumentProcessingSettings(),
    retry: false,
  });
  const [draft, setDraft] = useState<DocumentProcessingDraft | null>(null);
  const [badField, setBadField] = useState<string | null>(null);
  const [credentialModal, setCredentialModal] = useState<CredentialModalState | null>(null);
  const [credentialDraft, setCredentialDraft] = useState<CredentialDraft>(EMPTY_CREDENTIAL);
  const [deleteCredential, setDeleteCredential] = useState<DocumentProcessingCredentialStatus | null>(null);

  useEffect(() => {
    if (settingsQuery.data && draft === null) {
      setDraft(documentProcessingDraftFrom(settingsQuery.data));
    }
  }, [draft, settingsQuery.data]);

  const shown = draft ?? (settingsQuery.data ? documentProcessingDraftFrom(settingsQuery.data) : null);
  const storedDraft = useMemo(
    () => (settingsQuery.data ? documentProcessingDraftFrom(settingsQuery.data) : null),
    [settingsQuery.data],
  );
  const dirty = !!shown && !!storedDraft && JSON.stringify(shown) !== JSON.stringify(storedDraft);
  const credentials = settingsQuery.data?.mineru_credentials ?? [];
  const healthyCount = credentials.filter((credential) => credential.health?.ok).length;
  const enabledCount = credentials.filter((credential) => credential.enabled).length;
  const invalidate = () => queryClient.invalidateQueries({ queryKey: ['admin-document-processing-settings'] });

  const saveMutation = useMutation({
    mutationFn: (value: DocumentProcessingDraft) => api.setDocumentProcessingSettings(value),
    onSuccess: (saved) => {
      queryClient.setQueryData(['admin-document-processing-settings'], saved);
      setDraft(documentProcessingDraftFrom(saved));
      setBadField(null);
      toast(tr('文档处理设置已保存', 'Document-processing settings saved'), 'ok');
    },
    onError: (error) => toast(`${tr('保存失败', 'Save failed')}：${errorText(error)}`, 'error'),
  });

  const createMutation = useMutation({
    mutationFn: (input: DocumentProcessingCredentialCreate) => api.createDocumentProcessingCredential(input),
    onSuccess: () => {
      setCredentialModal(null);
      void invalidate();
      toast(tr('MinerU 密钥已加密保存', 'MinerU credential saved encrypted'), 'ok');
    },
    onError: (error) => toast(`${tr('保存失败', 'Save failed')}：${errorText(error)}`, 'error'),
  });

  const updateMutation = useMutation({
    mutationFn: ({ id, input }: { id: string; input: DocumentProcessingCredentialUpdate }) => api.updateDocumentProcessingCredential(id, input),
    onSuccess: () => {
      setCredentialModal(null);
      void invalidate();
      toast(tr('MinerU 密钥已更新', 'MinerU credential updated'), 'ok');
    },
    onError: (error) => toast(`${tr('更新失败', 'Update failed')}：${errorText(error)}`, 'error'),
  });

  const toggleMutation = useMutation({
    mutationFn: (credential: DocumentProcessingCredentialStatus) => api.updateDocumentProcessingCredential(credential.id, { enabled: !credential.enabled }),
    onSuccess: (credential) => {
      void invalidate();
      toast(credential.enabled ? tr('密钥已启用', 'Credential enabled') : tr('密钥已停用', 'Credential disabled'), 'ok');
    },
    onError: (error) => toast(`${tr('操作失败', 'Action failed')}：${errorText(error)}`, 'error'),
  });

  const testMutation = useMutation({
    mutationFn: (credential: DocumentProcessingCredentialStatus) => api.testDocumentProcessingCredential(credential.id),
    onSuccess: (result) => {
      void invalidate();
      toast(
        result.ok
          ? tr(`MinerU 连接成功，${result.latency_ms} ms`, `MinerU connected in ${result.latency_ms} ms`)
          : tr(`MinerU 连接失败：${result.detail}`, `MinerU connection failed: ${result.detail}`),
        result.ok ? 'ok' : 'error',
      );
    },
    onError: (error) => toast(`${tr('测试失败', 'Test failed')}：${errorText(error)}`, 'error'),
  });

  const deleteMutation = useMutation({
    mutationFn: (id: string) => api.deleteDocumentProcessingCredential(id),
    onSuccess: () => {
      setDeleteCredential(null);
      void invalidate();
      toast(tr('MinerU 密钥已删除', 'MinerU credential deleted'), 'ok');
    },
    onError: (error) => toast(`${tr('删除失败', 'Delete failed')}：${errorText(error)}`, 'error'),
  });

  const openCreate = () => {
    setCredentialDraft(EMPTY_CREDENTIAL);
    setCredentialModal({ mode: 'create' });
  };

  const openEdit = (credential: DocumentProcessingCredentialStatus) => {
    setCredentialDraft({ label: credential.label ?? '', secret: '', enabled: credential.enabled });
    setCredentialModal({ mode: 'edit', credential });
  };

  const saveCredential = () => {
    if (!credentialModal) return;
    const label = credentialDraft.label.trim() || null;
    if (credentialModal.mode === 'create') {
      if (!credentialDraft.secret.trim()) return;
      createMutation.mutate({ secret: credentialDraft.secret.trim(), label, enabled: credentialDraft.enabled });
      return;
    }
    const input: DocumentProcessingCredentialUpdate = { label, enabled: credentialDraft.enabled };
    if (credentialDraft.secret.trim()) input.secret = credentialDraft.secret.trim();
    updateMutation.mutate({ id: credentialModal.credential!.id, input });
  };

  const saveSettings = () => {
    if (!shown) return;
    const invalid = validateDocumentProcessingDraft(shown);
    setBadField(invalid);
    if (invalid) {
      toast(tr('请先修正文档处理设置', 'Fix the document-processing settings first'), 'error');
      return;
    }
    saveMutation.mutate(shown);
  };

  if (settingsQuery.isLoading) return <div className="empty">{tr('加载中…', 'Loading…')}</div>;
  if (settingsQuery.isError || !shown) {
    return (
      <div className="card card-pad empty">
        {tr('无法加载文档处理设置（后端不可用或无权限）', 'Failed to load document-processing settings')}
        <div style={{ marginTop: 10 }}>
          <button className="btn btn-soft sm" onClick={() => void settingsQuery.refetch()}>{tr('重试', 'Retry')}</button>
        </div>
      </div>
    );
  }

  const credentialBusy = createMutation.isPending || updateMutation.isPending;

  return (
    <div className="literature-settings-stack">
      <div className="literature-settings-summary">
        <div>
          <div className="section-h">
            <Icon name="file" size={15} style={{ color: 'var(--accent)' }} />
            {tr('PDF 解析运行策略', 'PDF processing policy')}
          </div>
          <div className="literature-settings-intro">
            {tr('进入论文库的 PDF 按此策略解析。MinerU 密钥按启用顺序轮询，失败后按设置重试或回退 PyMuPDF。', 'Library PDFs follow this policy. Enabled MinerU credentials rotate before the configured retry and PyMuPDF fallback policy is applied.')}
          </div>
        </div>
        <div className="literature-settings-stats" aria-label={tr('配置概览', 'Configuration summary')}>
          <div><strong>{enabledCount}</strong><span>{tr('启用密钥', 'enabled keys')}</span></div>
          <div><strong>{healthyCount}</strong><span>{tr('测试可用', 'healthy')}</span></div>
          <div><strong>{shown.mineru_concurrency}</strong><span>{tr('并发任务', 'concurrency')}</span></div>
        </div>
      </div>

      {badField === 'parser_policy' && (
        <div className="field-error">{tr('MinerU 与 PyMuPDF 至少启用一个。', 'Enable at least one parser.')}</div>
      )}

      <div className="settings-main-side">
        <section className="card card-pad">
          <div className="section-h" style={{ marginBottom: 16 }}>
            <Icon name="server" size={15} style={{ color: 'var(--accent)' }} />
            MinerU
          </div>
          <div className="settings-row">
            <div className="settings-row-text">
              <div className="field-label">{tr('启用 MinerU', 'Enable MinerU')}</div>
              <div className="field-hint">{tr('优先生成结构化 Markdown、图片与可定位正文。', 'Prefer structured Markdown, figures, and anchorable text.')}</div>
            </div>
            <Switch checked={shown.mineru_enabled} onChange={(mineru_enabled) => setDraft({ ...shown, mineru_enabled })} />
          </div>
          <FormField
            label={tr('API 根地址', 'API root')}
            error={badField === 'mineru_base_url' ? tr('请输入不含凭据、查询参数或片段的 HTTP(S) 地址', 'Enter an HTTP(S) root without credentials, query, or fragment') : null}
          >
            <input className="input mono" value={shown.mineru_base_url} onChange={(event) => setDraft({ ...shown, mineru_base_url: event.target.value })} />
          </FormField>
          <div className="settings-fields literature-settings-fields-compact" style={{ marginTop: 14 }}>
            <NumberField label={tr('解析超时（秒）', 'Parse timeout (s)')} value={shown.mineru_timeout_seconds} min={31} max={86400} error={badField === 'mineru_timeout_seconds'} onChange={(mineru_timeout_seconds) => setDraft({ ...shown, mineru_timeout_seconds })} />
            <NumberField label={tr('轮询间隔（秒）', 'Poll interval (s)')} value={shown.mineru_poll_interval_seconds} min={1} max={300} error={badField === 'mineru_poll_interval_seconds'} onChange={(mineru_poll_interval_seconds) => setDraft({ ...shown, mineru_poll_interval_seconds })} />
            <NumberField label={tr('失败重试次数', 'Retry count')} value={shown.mineru_retries} min={0} max={5} error={badField === 'mineru_retries'} integer onChange={(mineru_retries) => setDraft({ ...shown, mineru_retries })} />
            <NumberField label={tr('并发解析数', 'Parse concurrency')} value={shown.mineru_concurrency} min={1} max={16} error={badField === 'mineru_concurrency'} integer onChange={(mineru_concurrency) => setDraft({ ...shown, mineru_concurrency })} />
          </div>
        </section>

        <section className="card card-pad">
          <div className="section-h" style={{ marginBottom: 16 }}>
            <Icon name="layers" size={15} style={{ color: 'var(--accent)' }} />
            {tr('回退与处理边界', 'Fallback and boundaries')}
          </div>
          <div className="settings-row">
            <div className="settings-row-text">
              <div className="field-label">{tr('启用 PyMuPDF 回退', 'Enable PyMuPDF fallback')}</div>
              <div className="field-hint">{tr('MinerU 明确失败且重试耗尽后，仍提取纯文本供检索与引用。', 'Extract plain text for retrieval and citation after MinerU explicitly fails and exhausts retries.')}</div>
            </div>
            <Switch checked={shown.pymupdf_fallback_enabled} onChange={(pymupdf_fallback_enabled) => setDraft({ ...shown, pymupdf_fallback_enabled })} />
          </div>
          <div className="literature-settings-intro" style={{ marginTop: 18 }}>
            {tr('检索候选池中的 PDF 不在这里处理；只有已进入论文库的 PDF 才会创建解析与向量任务。', 'PDFs in the discovery candidate pool are not processed here. Parsing and embedding begin only after a PDF enters a library.')}
          </div>
        </section>
      </div>

      <section className="card card-pad">
        <div className="literature-credential-group-head">
          <div>
            <div className="section-h">
              <Icon name="shield" size={15} style={{ color: 'var(--accent)' }} />
              {tr('MinerU 密钥池', 'MinerU credential pool')}
            </div>
            <p>{tr('密钥加密保存且不回传明文。测试操作使用对应密钥访问当前 API 根地址。', 'Secrets are encrypted and never returned. Tests use the selected credential against the current API root.')}</p>
          </div>
          <button className="btn btn-primary sm" onClick={openCreate}>
            <Icon name="plus" size={12} />
            {tr('添加密钥', 'Add key')}
          </button>
        </div>
        {credentials.length === 0 ? (
          <div className="literature-credential-empty">{tr('尚未配置 MinerU 密钥。', 'No MinerU credential configured.')}</div>
        ) : (
          <div className="literature-credential-list">
            {credentials.map((credential) => {
              const testing = testMutation.isPending && testMutation.variables?.id === credential.id;
              const toggling = toggleMutation.isPending && toggleMutation.variables?.id === credential.id;
              return (
                <div className="literature-credential-row" key={credential.id}>
                  <div className="literature-credential-identity">
                    <div className="row gap8 wrap">
                      <strong>{credential.label || tr('未命名密钥', 'Unnamed credential')}</strong>
                      <code>{credential.preview}</code>
                      <HealthBadge health={credential.health} />
                      {!credential.enabled && <span className="pill sm">{tr('已停用', 'Disabled')}</span>}
                    </div>
                    <span>{credential.health ? `${credential.health.detail} · ${formatTime(credential.health.checked_at)}` : tr('保存后尚未测试', 'Not tested since it was saved')}</span>
                  </div>
                  <div className="row gap6 literature-credential-actions">
                    <Switch checked={credential.enabled} disabled={toggling} onChange={() => toggleMutation.mutate(credential)} />
                    <button className="btn btn-soft sm" disabled={testing} onClick={() => testMutation.mutate(credential)}>
                      <Icon name={testing ? 'refresh' : 'play'} size={12} style={testing ? { animation: 'spin 1s linear infinite' } : undefined} />
                      {testing ? tr('测试中…', 'Testing…') : tr('测试', 'Test')}
                    </button>
                    <button className="icon-btn" title={tr('编辑密钥', 'Edit credential')} onClick={() => openEdit(credential)}><Icon name="pen" size={13} /></button>
                    <button className="icon-btn" title={tr('删除密钥', 'Delete credential')} onClick={() => setDeleteCredential(credential)}><Icon name="trash" size={13} /></button>
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </section>

      <div className="literature-settings-savebar">
        <div>
          <strong>{dirty ? tr('有未保存的文档处理设置', 'Unsaved document-processing settings') : tr('文档处理设置已同步', 'Document-processing settings are in sync')}</strong>
          <span>{tr('密钥操作会单独即时保存。', 'Credential changes save immediately.')}</span>
        </div>
        <button className="btn btn-primary" disabled={!dirty || saveMutation.isPending} onClick={saveSettings}>
          {saveMutation.isPending ? tr('保存中…', 'Saving…') : tr('保存处理设置', 'Save processing settings')}
        </button>
      </div>

      <Modal
        open={credentialModal !== null}
        onClose={() => !credentialBusy && setCredentialModal(null)}
        title={credentialModal?.mode === 'edit' ? tr('编辑 MinerU 密钥', 'Edit MinerU credential') : tr('新增 MinerU 密钥', 'Add MinerU credential')}
        sub={tr('密钥只写不读', 'Secrets are write-only')}
        width={500}
        footer={<>
          <button className="btn btn-ghost" disabled={credentialBusy} onClick={() => setCredentialModal(null)}>{tr('取消', 'Cancel')}</button>
          <button className="btn btn-primary" disabled={credentialBusy || (credentialModal?.mode === 'create' && !credentialDraft.secret.trim())} onClick={saveCredential}>{credentialBusy ? tr('保存中…', 'Saving…') : tr('保存', 'Save')}</button>
        </>}
      >
        <FormField label={tr('密钥标签', 'Credential label')} hint={tr('用于区分同一池中的多个密钥。', 'Distinguishes credentials in the same pool.')}>
          <input className="input" maxLength={120} value={credentialDraft.label} placeholder={tr('可选', 'Optional')} onChange={(event) => setCredentialDraft({ ...credentialDraft, label: event.target.value })} />
        </FormField>
        <FormField label={tr(credentialModal?.mode === 'edit' ? '替换密钥' : 'API Key', credentialModal?.mode === 'edit' ? 'Replacement secret' : 'API key')} hint={credentialModal?.mode === 'edit' ? tr('留空保留当前密钥。', 'Leave empty to keep the current secret.') : tr('保存后只显示末四位。', 'Only the last four characters remain visible after saving.')}>
          <input className="input mono" type="password" autoComplete="new-password" value={credentialDraft.secret} placeholder={credentialModal?.mode === 'edit' ? tr('留空则不修改', 'Leave empty to keep') : '••••••••'} onChange={(event) => setCredentialDraft({ ...credentialDraft, secret: event.target.value })} />
        </FormField>
        <div className="settings-row">
          <div className="settings-row-text">
            <div className="field-label">{tr('启用此密钥', 'Enable this credential')}</div>
            <div className="field-hint">{tr('停用后保留配置，但不进入轮询池。', 'Disabled credentials remain stored but leave the rotation pool.')}</div>
          </div>
          <Switch checked={credentialDraft.enabled} onChange={(enabled) => setCredentialDraft({ ...credentialDraft, enabled })} />
        </div>
      </Modal>

      <ConfirmModal
        open={deleteCredential !== null}
        onClose={() => setDeleteCredential(null)}
        title={tr('删除 MinerU 密钥', 'Delete MinerU credential')}
        message={deleteCredential ? tr(`确定永久删除“${deleteCredential.label ?? deleteCredential.preview}”吗？删除后无法恢复。`, `Permanently delete “${deleteCredential.label ?? deleteCredential.preview}”? This cannot be undone.`) : ''}
        confirmText={tr('删除', 'Delete')}
        danger
        busy={deleteMutation.isPending}
        onConfirm={() => deleteCredential && deleteMutation.mutate(deleteCredential.id)}
      />
    </div>
  );
}

function NumberField({
  label,
  value,
  min,
  max,
  error,
  integer = false,
  onChange,
}: {
  label: string;
  value: number;
  min: number;
  max: number;
  error: boolean;
  integer?: boolean;
  onChange: (value: number) => void;
}) {
  return (
    <FormField label={label} error={error ? tr(`请输入 ${min}–${max} 范围内的数值`, `Enter a value from ${min} to ${max}`) : null}>
      <input className="input mono" type="number" min={min} max={max} step={integer ? 1 : 'any'} value={value} onChange={(event) => onChange(Number(event.target.value))} />
    </FormField>
  );
}
