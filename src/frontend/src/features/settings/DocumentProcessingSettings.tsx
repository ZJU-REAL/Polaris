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
import { SettingsGroup, SettingsRow, SettingsSection, SettingsStack, StatusDot } from './settingsUi';
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
      return tr('有设置无效，请检查标红的项', 'Some settings aren’t valid. Check the highlighted fields.');
    }
    if (code === 'INVALID_DOCUMENT_PROCESSING_CREDENTIAL') {
      return tr('MinerU 密钥无效', 'That MinerU key isn’t valid');
    }
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
  const invalidate = () => queryClient.invalidateQueries({ queryKey: ['admin-document-processing-settings'] });

  const saveMutation = useMutation({
    mutationFn: (value: DocumentProcessingDraft) => api.setDocumentProcessingSettings(value),
    onSuccess: (saved) => {
      queryClient.setQueryData(['admin-document-processing-settings'], saved);
      setDraft(documentProcessingDraftFrom(saved));
      setBadField(null);
      toast(tr('已保存', 'Saved'), 'ok');
    },
    onError: (error) => toast(`${tr('保存失败', 'Couldn’t save')}：${errorText(error)}`, 'error'),
  });

  const createMutation = useMutation({
    mutationFn: (input: DocumentProcessingCredentialCreate) => api.createDocumentProcessingCredential(input),
    onSuccess: () => {
      setCredentialModal(null);
      void invalidate();
      toast(tr('已保存密钥', 'Key saved'), 'ok');
    },
    onError: (error) => toast(`${tr('保存失败', 'Couldn’t save')}：${errorText(error)}`, 'error'),
  });

  const updateMutation = useMutation({
    mutationFn: ({ id, input }: { id: string; input: DocumentProcessingCredentialUpdate }) => api.updateDocumentProcessingCredential(id, input),
    onSuccess: () => {
      setCredentialModal(null);
      void invalidate();
      toast(tr('已保存', 'Saved'), 'ok');
    },
    onError: (error) => toast(`${tr('保存失败', 'Couldn’t save')}：${errorText(error)}`, 'error'),
  });

  const toggleMutation = useMutation({
    mutationFn: (credential: DocumentProcessingCredentialStatus) => api.updateDocumentProcessingCredential(credential.id, { enabled: !credential.enabled }),
    onSuccess: (credential) => {
      void invalidate();
      toast(credential.enabled ? tr('已启用', 'Turned on') : tr('已停用', 'Turned off'), 'ok');
    },
    onError: (error) => toast(`${tr('操作失败', 'Something went wrong')}：${errorText(error)}`, 'error'),
  });

  const testMutation = useMutation({
    mutationFn: (credential: DocumentProcessingCredentialStatus) => api.testDocumentProcessingCredential(credential.id),
    onSuccess: (result) => {
      void invalidate();
      toast(
        result.ok
          ? tr('MinerU 连接成功', 'MinerU connected')
          : tr(`MinerU 连接失败：${result.detail}`, `Couldn’t reach MinerU: ${result.detail}`),
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
      toast(tr('已删除', 'Deleted'), 'ok');
    },
    onError: (error) => toast(`${tr('删除失败', 'Couldn’t delete')}：${errorText(error)}`, 'error'),
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
      toast(tr('请先修改标红的设置', 'Fix the highlighted settings first'), 'error');
      return;
    }
    saveMutation.mutate(shown);
  };

  if (settingsQuery.isLoading || settingsQuery.isError || !shown) {
    return (
      <SettingsStack>
        <SettingsSection title={tr('PDF 解析', 'PDF parsing')}>
          <SettingsGroup>
            {settingsQuery.isLoading ? (
              <div className="st-row"><span className="st-row-hint">{tr('加载中…', 'Loading…')}</span></div>
            ) : (
              <SettingsRow label={tr('无法加载文档处理设置', 'Couldn’t load document settings')}>
                <button className="btn btn-ghost sm" onClick={() => void settingsQuery.refetch()}>{tr('重试', 'Retry')}</button>
              </SettingsRow>
            )}
          </SettingsGroup>
        </SettingsSection>
      </SettingsStack>
    );
  }

  const credentialBusy = createMutation.isPending || updateMutation.isPending;

  return (
    <SettingsStack>
      <SettingsSection
        title="MinerU"
        desc={badField === 'parser_policy'
          ? <span style={{ color: 'var(--danger-tx)' }}>{tr('MinerU 和 PyMuPDF 至少启用一个', 'Turn on MinerU, PyMuPDF or both')}</span>
          : tr('文献库中的 PDF 优先用 MinerU 解析，失败时改用 PyMuPDF。', 'Library PDFs are parsed with MinerU first, then PyMuPDF if that fails.')}
      >
        <SettingsGroup>
          <SettingsRow labelId="mineru-enabled" label={tr('使用 MinerU', 'Use MinerU')} hint={tr('保留版式、图片和公式', 'Keeps layout, figures and equations')}>
            <Switch checked={shown.mineru_enabled} onChange={(mineru_enabled) => setDraft({ ...shown, mineru_enabled })} aria-labelledby="mineru-enabled" />
          </SettingsRow>
          <SettingsRow
            label={tr('API 地址', 'API URL')}
            error={badField === 'mineru_base_url' ? tr('请输入以 http:// 或 https:// 开头的地址，不要带参数', 'Enter an http:// or https:// URL without parameters') : null}
          >
            <input className="input mono" value={shown.mineru_base_url} onChange={(event) => setDraft({ ...shown, mineru_base_url: event.target.value })} />
          </SettingsRow>
          <NumberField label={tr('解析超时（秒）', 'Parse timeout (s)')} value={shown.mineru_timeout_seconds} min={31} max={86400} error={badField === 'mineru_timeout_seconds'} onChange={(mineru_timeout_seconds) => setDraft({ ...shown, mineru_timeout_seconds })} />
          <NumberField label={tr('查询间隔（秒）', 'Check every (s)')} value={shown.mineru_poll_interval_seconds} min={1} max={300} error={badField === 'mineru_poll_interval_seconds'} onChange={(mineru_poll_interval_seconds) => setDraft({ ...shown, mineru_poll_interval_seconds })} />
          <NumberField label={tr('失败重试次数', 'Retries')} value={shown.mineru_retries} min={0} max={5} error={badField === 'mineru_retries'} integer onChange={(mineru_retries) => setDraft({ ...shown, mineru_retries })} />
          <NumberField label={tr('同时解析数', 'Parse at once')} value={shown.mineru_concurrency} min={1} max={16} error={badField === 'mineru_concurrency'} integer onChange={(mineru_concurrency) => setDraft({ ...shown, mineru_concurrency })} />
          <SettingsRow labelId="pymupdf-fallback" label={tr('MinerU 失败时用 PyMuPDF', 'Use PyMuPDF if MinerU fails')} hint={tr('只提取纯文本，仍可搜索和引用', 'Extracts plain text so the paper stays searchable')}>
            <Switch checked={shown.pymupdf_fallback_enabled} onChange={(pymupdf_fallback_enabled) => setDraft({ ...shown, pymupdf_fallback_enabled })} aria-labelledby="pymupdf-fallback" />
          </SettingsRow>
        </SettingsGroup>
      </SettingsSection>

      <SettingsSection
        title={tr('MinerU 密钥', 'MinerU keys')}
        desc={tr(`共 ${credentials.length} 个，${healthyCount} 个可用；多个密钥轮流使用，修改立即保存。`, `${credentials.length} keys, ${healthyCount} working. Several keys are used in turn. Changes save right away.`)}
        actions={
          <button className="btn btn-ghost sm" onClick={openCreate}>
            <Icon name="plus" size={12} />
            {tr('添加密钥', 'Add key')}
          </button>
        }
      >
        <SettingsGroup>
        {credentials.length === 0 ? (
          <div className="st-row"><span className="st-row-hint">{tr('还没有 MinerU 密钥', 'No MinerU keys yet')}</span></div>
        ) : (
          credentials.map((credential) => {
            const testing = testMutation.isPending && testMutation.variables?.id === credential.id;
            const toggling = toggleMutation.isPending && toggleMutation.variables?.id === credential.id;
            return (
              <div className="st-row" key={credential.id} style={{ flexWrap: 'wrap' }}>
                <span style={{ fontSize: 13 }}>{credential.label || tr('未命名', 'Unnamed')}</span>
                <code className="mono" style={{ fontSize: 12, color: 'var(--text-3)' }}>{credential.preview}</code>
                {credential.enabled
                  ? <HealthBadge health={credential.health} />
                  : <StatusDot tone="idle">{tr('已停用', 'Off')}</StatusDot>}
                <span style={{ flex: 1 }} />
                <button className="btn btn-ghost sm" disabled={testing} onClick={() => testMutation.mutate(credential)}>
                  {testing ? tr('测试中…', 'Testing…') : tr('测试', 'Test')}
                </button>
                <button className="icon-btn" title={tr('编辑', 'Edit')} aria-label={tr('编辑', 'Edit')} onClick={() => openEdit(credential)}><Icon name="pen" size={13} /></button>
                <button className="icon-btn st-quiet-danger" title={tr('删除', 'Delete')} aria-label={tr('删除', 'Delete')} onClick={() => setDeleteCredential(credential)}><Icon name="trash" size={13} /></button>
                <Switch checked={credential.enabled} disabled={toggling} onChange={() => toggleMutation.mutate(credential)} aria-label={tr('启用此密钥', 'Turn on this key')} />
              </div>
            );
          })
        )}
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
        title={credentialModal?.mode === 'edit' ? tr('编辑 MinerU 密钥', 'Edit MinerU key') : tr('添加 MinerU 密钥', 'Add MinerU key')}
        width={500}
        footer={<>
          <button className="btn btn-ghost" disabled={credentialBusy} onClick={() => setCredentialModal(null)}>{tr('取消', 'Cancel')}</button>
          <button className="btn btn-primary" disabled={credentialBusy || (credentialModal?.mode === 'create' && !credentialDraft.secret.trim())} onClick={saveCredential}>{credentialBusy ? tr('保存中…', 'Saving…') : tr('保存', 'Save')}</button>
        </>}
      >
        <FormField label={tr('名称', 'Name')} hint={tr('用来区分多个密钥', 'Tells your keys apart')}>
          <input className="input" maxLength={120} value={credentialDraft.label} placeholder={tr('例如 备用', 'e.g. Backup')} onChange={(event) => setCredentialDraft({ ...credentialDraft, label: event.target.value })} />
        </FormField>
        <FormField label={tr(credentialModal?.mode === 'edit' ? '新密钥' : 'API Key', credentialModal?.mode === 'edit' ? 'New key' : 'API key')} hint={credentialModal?.mode === 'edit' ? tr('留空则不修改', 'Leave empty to keep the current key') : tr('保存后只显示末 4 位', 'Only the last 4 characters are shown after saving')}>
          <input className="input mono" type="password" autoComplete="new-password" value={credentialDraft.secret} placeholder={'••••••••'} onChange={(event) => setCredentialDraft({ ...credentialDraft, secret: event.target.value })} />
        </FormField>
        <div className="settings-row">
          <div className="settings-row-text">
            <div className="field-label">{tr('启用', 'On')}</div>
            <div className="field-hint">{tr('停用的密钥会保留，但不会被使用', 'Turned-off keys are kept but not used')}</div>
          </div>
          <Switch checked={credentialDraft.enabled} onChange={(enabled) => setCredentialDraft({ ...credentialDraft, enabled })} />
        </div>
      </Modal>

      <ConfirmModal
        open={deleteCredential !== null}
        onClose={() => setDeleteCredential(null)}
        title={deleteCredential ? tr(`删除密钥「${deleteCredential.label ?? deleteCredential.preview}」？`, `Delete key “${deleteCredential.label ?? deleteCredential.preview}”?`) : ''}
        message={tr('删除后无法恢复。', 'This can’t be undone.')}
        confirmText={tr('删除', 'Delete')}
        danger
        busy={deleteMutation.isPending}
        onConfirm={() => deleteCredential && deleteMutation.mutate(deleteCredential.id)}
      />
    </SettingsStack>
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
    <SettingsRow label={label} error={error ? tr(`请输入 ${min}–${max} 之间的数`, `Enter a number from ${min} to ${max}`) : null}>
      <input className="input mono st-num" type="number" min={min} max={max} step={integer ? 1 : 'any'} value={value} onChange={(event) => onChange(Number(event.target.value))} />
    </SettingsRow>
  );
}
