import { useState } from 'react';
import { Icon } from './Icon';
import { Modal } from './Modal';
import { toast } from './Toast';
import { fmtTime } from '../../lib/format';
import { openExternal, type UpdateInfo } from '../../lib/host';
import { invokeJob } from '../../lib/host-jobs';
import { Markdown } from '../../lib/markdown';
import { tr } from '../../lib/i18n';

/**
 * 「有新版本」对话框：更新说明 + 查看发布页 + 立即更新。
 *
 * 顶栏的自动提示（UpdateBadge）和设置 → 关于里的手动检查（#812）共用这一个。
 * 两份的话，下次改的时候必然只改一处——比如这次加的「查看发布页」按钮。
 *
 * 两种更新方式由主进程判定（见 src/desktop/src/main/updates）：
 * - hot：只有界面变了，下载完直接换上并 reload，**不重启**；
 * - full：preload/主进程也变了，只能装安装器，Windows 会自动拉起、macOS 打开 dmg。
 */
export function UpdateDialog({
  info,
  open,
  onClose,
}: {
  info: UpdateInfo;
  open: boolean;
  onClose: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [percent, setPercent] = useState(0);

  const start = () => {
    setBusy(true);
    setPercent(0);
    invokeJob('host.update.apply', undefined, {
      onProgress: (p) => setPercent(p.total ? Math.round((p.done / p.total) * 100) : 0),
      onDone: (result) => {
        const hot = (result as { reloaded?: boolean } | null)?.reloaded === true;
        setBusy(false);
        onClose();
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
    <Modal
      open={open}
      onClose={() => !busy && onClose()}
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
          {info.releaseUrl && !busy && (
            <button
              className="btn btn-ghost"
              style={{ marginRight: 'auto' }}
              onClick={() => void openExternal(info.releaseUrl!)}
            >
              <Icon name="link" size={14} />
              {tr('查看发布页', 'View release')}
            </button>
          )}
          <button className="btn" onClick={onClose} disabled={busy}>
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
  );
}
