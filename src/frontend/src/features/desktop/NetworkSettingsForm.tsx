import { useEffect, useState } from 'react';
import { getNetworkSettings, setNetworkSettings, type NetworkSettings } from '../../lib/host';
import { tr } from '../../lib/i18n';

/** 主进程的校验错误（ERR_INVALID_PARAMS: …）→ 给用户看的一句话。 */
export function networkErrorText(error: unknown): string {
  const raw = error instanceof Error ? error.message : String(error);
  if (raw.includes('ERR_INVALID_PARAMS')) {
    return tr(
      '代理地址格式不对，例如 http://127.0.0.1:7890',
      'That proxy address doesn’t look right. Example: http://127.0.0.1:7890',
    );
  }
  return tr('无法保存', 'Couldn’t save');
}

/** 镜像选项；模块顶层不能调 tr，所以是函数。 */
export function mirrorOptions(): { v: NetworkSettings['mirror']; label: string }[] {
  return [
    { v: 'auto', label: tr('自动', 'Automatic') },
    { v: 'on', label: tr('优先使用', 'Prefer') },
    { v: 'off', label: tr('不使用', 'Never') },
  ];
}

/**
 * 首次安装（下载 Python 与组件）用的代理与国内镜像设置。安装失败页和
 * 设置 → 关于用的是同一个表单。改动在下次安装时生效，所以保存后提示重新打开。
 * 老外壳没有这项设置：读不到就整块不渲染。
 */
export function NetworkSettingsForm({
  initial,
  onSaved,
}: {
  /** 仅测试用：跳过读取，直接画出这份设置 */
  initial?: NetworkSettings;
  onSaved?: (saved: NetworkSettings) => void;
}) {
  const [loaded, setLoaded] = useState<NetworkSettings | null>(initial ?? null);
  const [proxy, setProxy] = useState(initial?.proxy ?? '');
  const [mirror, setMirror] = useState<NetworkSettings['mirror']>(initial?.mirror ?? 'auto');
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    if (initial) return;
    let alive = true;
    void getNetworkSettings().then((settings) => {
      if (!alive || !settings) return;
      setLoaded(settings);
      setProxy(settings.proxy);
      setMirror(settings.mirror);
    });
    return () => {
      alive = false;
    };
  }, [initial]);

  if (!loaded) return null;

  const dirty = proxy.trim() !== loaded.proxy || mirror !== loaded.mirror;

  const save = async () => {
    setSaving(true);
    setError('');
    setSaved(false);
    try {
      const next = await setNetworkSettings({ proxy: proxy.trim(), mirror });
      setLoaded(next);
      setProxy(next.proxy);
      setMirror(next.mirror);
      setSaved(true);
      onSaved?.(next);
    } catch (e) {
      setError(networkErrorText(e));
    } finally {
      setSaving(false);
    }
  };

  return (
    <div style={{ display: 'grid', gap: 10 }}>
      <label style={{ display: 'grid', gap: 4 }}>
        <span style={{ fontSize: 12, fontWeight: 500, color: 'var(--text-2)' }}>{tr('代理', 'Proxy')}</span>
        <input
          className="input mono"
          value={proxy}
          placeholder={tr('例如 http://127.0.0.1:7890', 'e.g. http://127.0.0.1:7890')}
          spellCheck={false}
          autoCapitalize="off"
          autoCorrect="off"
          onChange={(e) => {
            setProxy(e.target.value);
            setSaved(false);
          }}
        />
        <span style={{ fontSize: 12, color: 'var(--text-3)' }}>
          {tr('留空则使用系统代理', 'Leave empty to use the system proxy')}
        </span>
      </label>
      <label style={{ display: 'grid', gap: 4 }}>
        <span style={{ fontSize: 12, fontWeight: 500, color: 'var(--text-2)' }}>
          {tr('国内镜像', 'China mirrors')}
        </span>
        <select
          className="input"
          value={mirror}
          onChange={(e) => {
            setMirror(e.target.value as NetworkSettings['mirror']);
            setSaved(false);
          }}
        >
          {mirrorOptions().map((option) => (
            <option key={option.v} value={option.v}>
              {option.label}
            </option>
          ))}
        </select>
        <span style={{ fontSize: 12, color: 'var(--text-3)' }}>
          {tr('「自动」会在默认下载源连不上时改用镜像', '“Automatic” switches to mirrors when the default sources can’t be reached')}
        </span>
      </label>
      <div className="row" style={{ gap: 10, alignItems: 'center' }}>
        <button type="button" className="btn btn-ghost sm" disabled={!dirty || saving} onClick={() => void save()}>
          {saving ? tr('保存中…', 'Saving…') : tr('保存', 'Save')}
        </button>
        {error && <span role="alert" style={{ fontSize: 12, color: 'var(--danger-tx)' }}>{error}</span>}
        {saved && !error && (
          <span role="status" style={{ fontSize: 12, color: 'var(--text-3)' }}>
            {tr('已保存，重新打开 Polaris 后生效', 'Saved. Restart Polaris to apply.')}
          </span>
        )}
      </div>
    </div>
  );
}
