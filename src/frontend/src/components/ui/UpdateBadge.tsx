import { useEffect, useState } from 'react';
import { Icon } from './Icon';
import { Modal } from './Modal';
import { toast } from './Toast';
import { fmtTime } from '../../lib/format';
import { checkUpdate, type UpdateInfo } from '../../lib/host';
import { invokeJob } from '../../lib/host-jobs';
import { Markdown } from '../../lib/markdown';
import { tr } from '../../lib/i18n';

/** 启动后延迟一会儿再查，别和首屏的数据请求抢；之后每 6 小时查一次。 */
const FIRST_CHECK_MS = 20_000;
const INTERVAL_MS = 6 * 60 * 60 * 1000;

/**
 * 有新版本时在顶栏显示一个向上的箭头，点开是更新说明 + 确认/取消。
 *
 * 两种更新方式由主进程判定（见 src/desktop/src/main/updates）：
 * - hot：只有界面变了，下载完直接换上并 reload，**不重启**；
 * - full：preload/主进程也变了，只能装安装器，Windows 会自动拉起、macOS 打开 dmg。
 *
 * web 端 checkUpdate() 返回 null，整个组件不渲染。
 */
export function UpdateBadge() {
  const [info, setInfo] = useState<UpdateInfo | null>(null);
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [percent, setPercent] = useState(0);

  useEffect(() => {
    let cancelled = false;
    const run = async () => {
      const next = await checkUpdate();
      if (!cancelled && next?.available) setInfo(next);
    };
    const first = setTimeout(() => void run(), FIRST_CHECK_MS);
    const timer = setInterval(() => void run(), INTERVAL_MS);
    return () => {
      cancelled = true;
      clearTimeout(first);
      clearInterval(timer);
    };
  }, []);

  if (!info?.available) return null;

  const start = () => {
    setBusy(true);
    setPercent(0);
    invokeJob('host.update.apply', undefined, {
      onProgress: (p) => setPercent(p.total ? Math.round((p.done / p.total) * 100) : 0),
      onDone: (result) => {
        const hot = (result as { reloaded?: boolean } | null)?.reloaded === true;
        setBusy(false);
        setOpen(false);
        // 热更新走到这里时窗口已经在重载，提示多半来不及看到——不影响正确性
        if (!hot) {
          toast(
            tr('安装包已下载，按提示完成安装', 'Installer downloaded — follow the prompts'),
            'ok',
          );
        }
      },
      onError: (_code, message) => {
        setBusy(false);
        toast(`${tr('更新失败', 'Update failed')}：${message}`, 'error');
      },
    });
  };

  const hot = info.kind === 'hot';

  return (
    <>
      <button
        className="icon-btn update-badge"
        onClick={() => setOpen(true)}
        title={`${tr('有新版本', 'Update available')} ${info.latestVersion ?? ''}`}
        aria-label={tr('有新版本', 'Update available')}
      >
        {/* 没有专门的上箭头图标，用 chevron 旋转 */}
        <Icon name="chevron" size={16} style={{ transform: 'rotate(-90deg)' }} />
        <span className="badge dot-badge" />
      </button>

      <Modal
        open={open}
        onClose={() => !busy && setOpen(false)}
        title={`${tr('有新版本', 'Update available')} · v${info.latestVersion}`}
        sub={
          <>
            {tr('当前', 'Current')} v{info.currentVersion}
            {info.publishedAt ? ` · ${fmtTime(info.publishedAt)}` : ''}
            {' · '}
            {hot
              ? tr('本次更新无需重启', 'Applies without restarting')
              : tr('本次更新需要重新安装', 'Requires reinstalling the app')}
          </>
        }
        width={600}
        footer={
          <div className="row gap8" style={{ justifyContent: 'flex-end' }}>
            {busy && (
              <span style={{ marginRight: 'auto', fontSize: 12.5, color: 'var(--text-3)' }}>
                {tr('下载中', 'Downloading')} {percent}%
              </span>
            )}
            <button className="btn" onClick={() => setOpen(false)} disabled={busy}>
              {tr('取消', 'Cancel')}
            </button>
            <button className="btn btn-primary" onClick={start} disabled={busy}>
              <Icon name="download" size={14} />
              {busy ? tr('更新中…', 'Updating…') : tr('立即更新', 'Update now')}
            </button>
          </div>
        }
      >
        {info.notes ? (
          <Markdown source={info.notes} />
        ) : (
          <p style={{ color: 'var(--text-3)' }}>{tr('本次更新没有说明', 'No release notes')}</p>
        )}
      </Modal>
    </>
  );
}
