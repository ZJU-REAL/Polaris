import { useState } from 'react';
import { useMutation } from '@tanstack/react-query';
import { Modal } from '../../components/ui/Modal';
import { toast } from '../../components/ui/Toast';
import { api } from '../../lib/api';
import { copyText } from '../../lib/clipboard';
import { engineOrigin, localOrigin } from '../../lib/endpoint';
import { tr } from '../../lib/i18n';
import { SettingsActions, SettingsGroup, SettingsRow, SettingsSection, SettingsStack } from './settingsUi';

export function ExtensionApiKeySettings() {
  const [apiKey, setApiKey] = useState<string | null>(null);
  const [keyPrefix, setKeyPrefix] = useState<string | null>(null);
  const [revokeOpen, setRevokeOpen] = useState(false);
  // 浏览器扩展跑在同一台电脑上：给本机引擎地址，别的机器访问不到——附一行
  // 说明（#721）。
  const isLocalBase = localOrigin() != null;
  const baseUrl = engineOrigin();

  const rotateMutation = useMutation({
    mutationFn: () => api.rotateDownloadApiKey(),
    onSuccess: (result) => {
      setApiKey(result.api_key);
      setKeyPrefix(result.key_prefix);
      toast(
        tr('已生成新密钥，旧密钥已失效', 'New key created. The old key no longer works.'),
        'ok',
      );
    },
    onError: (error) => {
      const message = error instanceof Error ? error.message : String(error);
      toast(`${tr('生成失败', 'Couldn’t create a key')}：${message}`, 'error');
    },
  });

  const revokeMutation = useMutation({
    mutationFn: () => api.revokeDownloadApiKey(),
    onSuccess: () => {
      setApiKey(null);
      setKeyPrefix(null);
      setRevokeOpen(false);
      toast(tr('已撤销密钥', 'Key revoked'), 'ok');
    },
    onError: (error) => {
      const message = error instanceof Error ? error.message : String(error);
      toast(`${tr('撤销失败', 'Couldn’t revoke')}：${message}`, 'error');
    },
  });

  const testMutation = useMutation({
    mutationFn: () => api.testDownloadApiKey(apiKey!),
    onSuccess: () => {
      toast(tr('连接成功', 'Connected'), 'ok');
    },
    onError: (error) => {
      const message = error instanceof Error ? error.message : String(error);
      toast(`${tr('连接失败', 'Connection failed')}：${message}`, 'error');
    },
  });

  async function copy(value: string, label: string) {
    const copied = await copyText(value);
    toast(
      copied
        ? tr('已复制', 'Copied')
        : tr(`无法复制${label}，请手动复制`, `Couldn’t copy the ${label}. Copy it manually.`),
      copied ? 'ok' : 'error',
    );
  }

  return (
    <SettingsStack>
      <SettingsSection
        title={tr('浏览器扩展', 'Browser extension')}
        desc={tr('连接 Polaris 浏览器扩展，让它把论文 PDF 下载到文献库。', 'Connect the Polaris browser extension so it can download paper PDFs into your libraries.')}
      >
        <SettingsGroup>
          <SettingsRow
            label={tr('Polaris 地址', 'Polaris URL')}
            hint={isLocalBase ? tr('仅这台电脑上的浏览器可以使用', 'Works only in browsers on this computer') : undefined}
          >
            <code className="mono" style={{ fontSize: 12, color: 'var(--text-2)', overflowWrap: 'anywhere' }}>{baseUrl}</code>
            <button
              className="btn btn-ghost sm"
              title={tr('复制地址', 'Copy URL')}
              aria-label={tr('复制地址', 'Copy URL')}
              onClick={() => void copy(baseUrl, tr('地址', 'URL'))}
            >
              {tr('复制', 'Copy')}
            </button>
          </SettingsRow>
          {apiKey ? (
            <SettingsRow
              stack
              label={tr('密钥', 'Key')}
              hint={<span style={{ color: 'var(--warn-tx)' }}>{tr('密钥只显示这一次，请现在复制到扩展中', 'This key is shown only once. Copy it into the extension now.')}</span>}
            >
              <div className="row gap8">
                <input className="input mono" value={apiKey} readOnly style={{ flex: 1, minWidth: 0 }} />
                <button
                  className="btn btn-ghost sm"
                  title={tr('复制密钥', 'Copy key')}
                  aria-label={tr('复制密钥', 'Copy key')}
                  onClick={() => void copy(apiKey, tr('密钥', 'key'))}
                >
                  {tr('复制', 'Copy')}
                </button>
                <button
                  className="btn btn-ghost sm"
                  disabled={testMutation.isPending}
                  onClick={() => testMutation.mutate()}
                >
                  {testMutation.isPending ? tr('测试中…', 'Testing…') : tr('测试连接', 'Test connection')}
                </button>
              </div>
            </SettingsRow>
          ) : (
            <SettingsRow
              label={tr('密钥', 'Key')}
              hint={tr('已有的密钥不会显示，扩展里保存的密钥在撤销前一直有效', 'Existing keys aren’t shown. A saved key works until you revoke it.')}
            />
          )}
        </SettingsGroup>
        <SettingsActions note={keyPrefix ? `${tr('密钥前缀', 'Key prefix')}：${keyPrefix}` : undefined}>
          <button
            className="btn btn-ghost sm st-quiet-danger"
            disabled={revokeMutation.isPending}
            onClick={() => setRevokeOpen(true)}
          >
            {tr('撤销密钥', 'Revoke key')}
          </button>
          <button
            className="btn btn-primary sm"
            disabled={rotateMutation.isPending}
            onClick={() => rotateMutation.mutate()}
          >
            {rotateMutation.isPending ? tr('生成中…', 'Creating…') : tr('生成新密钥', 'Create new key')}
          </button>
        </SettingsActions>
      </SettingsSection>

      <Modal
        open={revokeOpen}
        onClose={() => !revokeMutation.isPending && setRevokeOpen(false)}
        title={tr('撤销扩展密钥？', 'Revoke the extension key?')}
        width={440}
        footer={
          <>
            <button className="btn btn-ghost sm" disabled={revokeMutation.isPending} onClick={() => setRevokeOpen(false)}>
              {tr('取消', 'Cancel')}
            </button>
            <button className="btn btn-danger sm" disabled={revokeMutation.isPending} onClick={() => revokeMutation.mutate()}>
              {revokeMutation.isPending ? tr('撤销中…', 'Revoking…') : tr('撤销', 'Revoke')}
            </button>
          </>
        }
      >
        <div style={{ fontSize: 12.5, color: 'var(--text-2)', lineHeight: 1.7 }}>
          {tr('浏览器扩展会立即断开，重新连接需要生成新密钥。', 'The browser extension disconnects right away. Create a new key to reconnect.')}
        </div>
      </Modal>
    </SettingsStack>
  );
}
