import { useState, type FormEvent } from 'react';
import { Icon } from '../../components/ui/Icon';
import { LangToggle } from '../../components/ui/LangToggle';
import { PolarisMark, PolarisWordmark } from '../../components/ui/PolarisLogo';
import { setToken } from '../../lib/api';
import { serverOrigin } from '../../lib/endpoint';
import { setServerUrl, testServer, type ServerProbe } from '../../lib/host';
import { tr } from '../../lib/i18n';

/**
 * 桌面端首启（或从菜单「Server…」进入）的服务器配置页。
 *
 * 做在前端而不是原生窗口：这样能直接复用 tokens.css 的浙大蓝、tr() 中英切换
 * 与登录页的表单样式；原生窗口要给 vite 加第二个 HTML entry，桌面端产物就与
 * web 分叉了。
 */
export function ServerSetupPage({ onCancel }: { onCancel?: () => void }) {
  const [url, setUrl] = useState(serverOrigin());
  const [probe, setProbe] = useState<ServerProbe | null>(null);
  const [testing, setTesting] = useState(false);
  const [saving, setSaving] = useState(false);

  function probeMessage(p: ServerProbe): string {
    if (p.ok) return `${tr('已连接 Polaris', 'Connected to Polaris')} v${p.version}`;
    switch (p.reason) {
      case 'invalid-url':
        return tr('地址格式不对，需要 http:// 或 https:// 开头', 'Invalid address — it must start with http:// or https://');
      case 'timeout':
        return tr('连接超时，请检查网络或 VPN', 'Connection timed out — check your network or VPN');
      case 'not-polaris':
        return tr('这个地址有响应，但不是 Polaris 服务器', 'Something answered, but it is not a Polaris server');
      default:
        return tr('连不上这个地址', 'Cannot reach this address');
    }
  }

  async function runTest(): Promise<ServerProbe | null> {
    setTesting(true);
    setProbe(null);
    try {
      const result = await testServer(url);
      setProbe(result);
      return result;
    } finally {
      setTesting(false);
    }
  }

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    const result = probe?.ok ? probe : await runTest();
    if (!result?.ok) return;
    setSaving(true);
    // 换服务器必须清掉旧 token：旧服务器签发的 JWT 在新服务器上一律 401，
    // 会把用户卡在「进首页 → 401 → 跳登录」的循环里。
    setToken(null);
    await setServerUrl(url);
    // 主进程随即销毁并重建窗口（reload 刷不掉 preload 注入值与 CSP），
    // 所以这之后的代码通常不会执行。
    setSaving(false);
  }

  return (
    <div className="auth-page">
      <div className="auth-brand">
        <PolarisMark size={56} />
        <PolarisWordmark height={32} />
      </div>

      <div style={{ position: 'absolute', top: 18, right: 20 }}>
        <LangToggle />
      </div>

      <div className="auth-card fadeup">
        <div className="auth-card-title">{tr('连接到服务器', 'Connect to a server')}</div>
        <div className="auth-card-sub">
          {tr(
            '桌面端本身不存数据，请填写你所在实验室的 Polaris 服务器地址',
            'The desktop app stores no data of its own — enter your lab’s Polaris server address',
          )}
        </div>

        <form onSubmit={(e) => void onSubmit(e)}>
          <div className="auth-field">
            <label htmlFor="server-url">{tr('服务器地址', 'Server address')}</label>
            <input
              id="server-url"
              className="auth-input"
              type="url"
              inputMode="url"
              autoFocus
              spellCheck={false}
              placeholder="https://polaris.example.edu"
              value={url}
              onChange={(e) => {
                setUrl(e.target.value);
                setProbe(null);
              }}
            />
          </div>

          {probe && (
            <div
              className={probe.ok ? undefined : 'auth-error'}
              style={
                probe.ok
                  ? { fontSize: 12.5, color: 'var(--ok-tx)', marginBottom: 12 }
                  : undefined
              }
            >
              {probeMessage(probe)}
              {!probe.ok && probe.detail ? `（${probe.detail}）` : ''}
            </div>
          )}

          <div className="row" style={{ gap: 10, marginTop: 6 }}>
            {onCancel && (
              <button type="button" className="btn" style={{ height: 38 }} onClick={onCancel}>
                {tr('取消', 'Cancel')}
              </button>
            )}
            <button
              type="button"
              className="btn"
              style={{ height: 38 }}
              disabled={!url || testing}
              onClick={() => void runTest()}
            >
              {testing ? (
                <Icon name="refresh" size={14} style={{ animation: 'spin 1s linear infinite' }} />
              ) : (
                <Icon name="search" size={14} />
              )}
              {tr('测试连接', 'Test')}
            </button>
            <button
              type="submit"
              className="btn btn-primary"
              disabled={!url || testing || saving}
              style={{ flex: 1, justifyContent: 'center', height: 38 }}
            >
              <Icon name="arrow" size={14} />
              {tr('保存并进入', 'Save and continue')}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
