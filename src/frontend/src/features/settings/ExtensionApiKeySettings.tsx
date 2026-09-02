import { useState } from 'react';
import { useMutation } from '@tanstack/react-query';
import { FormField } from '../../components/ui/FormField';
import { Icon } from '../../components/ui/Icon';
import { Modal } from '../../components/ui/Modal';
import { toast } from '../../components/ui/Toast';
import { api } from '../../lib/api';
import { copyText } from '../../lib/clipboard';
import { portalUrl } from '../../lib/endpoint';
import { tr } from '../../lib/i18n';

export function ExtensionApiKeySettings() {
  const [apiKey, setApiKey] = useState<string | null>(null);
  const [keyPrefix, setKeyPrefix] = useState<string | null>(null);
  const [revokeOpen, setRevokeOpen] = useState(false);
  const baseUrl = portalUrl();

  const rotateMutation = useMutation({
    mutationFn: () => api.rotateDownloadApiKey(),
    onSuccess: (result) => {
      setApiKey(result.api_key);
      setKeyPrefix(result.key_prefix);
      toast(
        tr('API Key 已生成，之前的密钥已失效', 'API key created. The previous key is now invalid.'),
        'ok',
      );
    },
    onError: (error) => {
      const message = error instanceof Error ? error.message : String(error);
      toast(`${tr('生成失败', 'Generation failed')}: ${message}`, 'error');
    },
  });

  const revokeMutation = useMutation({
    mutationFn: () => api.revokeDownloadApiKey(),
    onSuccess: () => {
      setApiKey(null);
      setKeyPrefix(null);
      setRevokeOpen(false);
      toast(tr('Polaris 扩展 API Key 已撤销', 'Polaris extension API key revoked'), 'ok');
    },
    onError: (error) => {
      const message = error instanceof Error ? error.message : String(error);
      toast(`${tr('撤销失败', 'Revocation failed')}: ${message}`, 'error');
    },
  });

  const testMutation = useMutation({
    mutationFn: () => api.testDownloadApiKey(apiKey!),
    onSuccess: (identity) => {
      toast(
        tr(`连接成功：${identity.email}`, `Connected as ${identity.email}`),
        'ok',
      );
    },
    onError: (error) => {
      const message = error instanceof Error ? error.message : String(error);
      toast(`${tr('连接测试失败', 'Connection test failed')}: ${message}`, 'error');
    },
  });

  async function copy(value: string, label: string) {
    const copied = await copyText(value);
    toast(
      copied
        ? tr(`${label} 已复制`, `${label} copied`)
        : tr(`${label} 复制失败，请手动复制`, `Could not copy ${label}. Copy it manually.`),
      copied ? 'ok' : 'error',
    );
  }

  return (
    <>
      <section className="card card-pad" style={{ maxWidth: 880 }}>
        <div
          className="row"
          style={{ justifyContent: 'space-between', alignItems: 'flex-start', gap: 16, flexWrap: 'wrap' }}
        >
          <div style={{ flex: '1 1 420px', minWidth: 0 }}>
            <div className="section-h">
              <Icon name="download" size={15} style={{ color: 'var(--accent)' }} />
              {tr('Polaris 扩展', 'Polaris extension')}
            </div>
            <div style={{ fontSize: 12, color: 'var(--text-3)', marginTop: 5, lineHeight: 1.65 }}>
              {tr(
                '使用用户专属 API Key 连接浏览器扩展。扩展可以领取论文下载任务，并将验真的 PDF 归档到指定文献库和论文。',
                'Connect the browser extension with a personal API key. The extension can claim paper download jobs and archive verified PDFs to their assigned libraries and papers.',
              )}
            </div>
          </div>
          <button
            className="btn btn-primary"
            disabled={rotateMutation.isPending}
            onClick={() => rotateMutation.mutate()}
          >
            <Icon name="refresh" size={14} />
            {rotateMutation.isPending
              ? tr('生成中...', 'Generating...')
              : apiKey
                ? tr('轮换 API Key', 'Rotate API key')
                : tr('生成 / 轮换 API Key', 'Create / rotate API key')}
          </button>
        </div>

        <div style={{ marginTop: 20 }}>
          <FormField label={tr('Polaris 地址', 'Polaris base URL')}>
            <div className="row gap8" style={{ alignItems: 'stretch' }}>
              <input className="input mono" value={baseUrl} readOnly style={{ flex: 1, minWidth: 0 }} />
              <button
                className="btn btn-ghost"
                title={tr('复制 Polaris 地址', 'Copy Polaris base URL')}
                aria-label={tr('复制 Polaris 地址', 'Copy Polaris base URL')}
                onClick={() => void copy(baseUrl, 'Base URL')}
              >
                <Icon name="link" size={15} />
              </button>
            </div>
          </FormField>
        </div>

        {apiKey ? (
          <div style={{ marginTop: 16 }}>
            <FormField label={tr('API Key（仅本次显示）', 'API key (shown once)')}>
              <div className="row gap8" style={{ alignItems: 'stretch' }}>
                <input className="input mono" value={apiKey} readOnly style={{ flex: 1, minWidth: 0 }} />
                <button
                  className="btn btn-ghost"
                  title={tr('复制 API Key', 'Copy API key')}
                  aria-label={tr('复制 API Key', 'Copy API key')}
                  onClick={() => void copy(apiKey, 'API Key')}
                >
                  <Icon name="link" size={15} />
                </button>
              </div>
            </FormField>
            <div style={{ fontSize: 12, color: 'var(--danger-tx)', marginTop: 8, lineHeight: 1.6 }}>
              {tr(
                '关闭或刷新页面后无法再次查看。生成新 Key 会立即使之前的 Key 失效。',
                'You cannot view this key again after closing or refreshing the page. Creating a new key immediately invalidates the previous key.',
              )}
            </div>
            <button
              className="btn btn-soft sm"
              disabled={testMutation.isPending}
              onClick={() => testMutation.mutate()}
              style={{ marginTop: 12 }}
            >
              <Icon name={testMutation.isPending ? 'refresh' : 'check'} size={13} />
              {testMutation.isPending ? tr('测试中...', 'Testing...') : tr('测试连接', 'Test connection')}
            </button>
          </div>
        ) : (
          <div
            style={{
              marginTop: 18,
              padding: 14,
              background: 'var(--surface-2)',
              border: '1px solid var(--border)',
              borderRadius: 6,
              color: 'var(--text-3)',
              fontSize: 12,
              lineHeight: 1.65,
            }}
          >
            {tr(
              'Polaris 不保存可恢复的明文密钥，因此此页面不会显示现有 Key 的状态。扩展中已保存的 Key 会继续有效，直到你轮换或撤销它。',
              'Polaris does not retain recoverable plaintext keys, so this page does not claim whether a key already exists. A key saved in the extension remains valid until you rotate or revoke it.',
            )}
          </div>
        )}

        <div
          className="row"
          style={{ justifyContent: 'space-between', alignItems: 'center', gap: 12, marginTop: 20, flexWrap: 'wrap' }}
        >
          <span className="mono" style={{ fontSize: 11.5, color: 'var(--text-3)', overflowWrap: 'anywhere' }}>
            {keyPrefix
              ? `${tr('本次生成的密钥前缀', 'New key prefix')}: ${keyPrefix}`
              : tr('权限：领取任务、更新状态、上传 PDF', 'Scopes: claim jobs, update status, upload PDFs')}
          </span>
          <button
            className="btn btn-ghost"
            disabled={revokeMutation.isPending}
            onClick={() => setRevokeOpen(true)}
            style={{ color: 'var(--danger-tx)' }}
          >
            <Icon name="trash" size={14} />
            {tr('撤销 API Key', 'Revoke API key')}
          </button>
        </div>
      </section>

      <Modal
        open={revokeOpen}
        onClose={() => !revokeMutation.isPending && setRevokeOpen(false)}
        title={tr('撤销 Polaris 扩展 API Key', 'Revoke Polaris extension API key')}
        sub={tr('此操作会立即断开使用当前 Key 的扩展', 'This immediately disconnects extensions using the current key')}
        width={440}
        footer={
          <>
            <button className="btn btn-ghost sm" disabled={revokeMutation.isPending} onClick={() => setRevokeOpen(false)}>
              {tr('取消', 'Cancel')}
            </button>
            <button className="btn btn-danger sm" disabled={revokeMutation.isPending} onClick={() => revokeMutation.mutate()}>
              {revokeMutation.isPending ? tr('撤销中...', 'Revoking...') : tr('确认撤销', 'Revoke key')}
            </button>
          </>
        }
      >
        <div style={{ fontSize: 12.5, color: 'var(--text-2)', lineHeight: 1.7 }}>
          {tr(
            '撤销后，浏览器扩展将无法领取任务或上传 PDF。需要再次连接时，请生成新的 API Key。',
            'After revocation, the browser extension cannot claim jobs or upload PDFs. Generate a new API key before reconnecting it.',
          )}
        </div>
      </Modal>
    </>
  );
}
