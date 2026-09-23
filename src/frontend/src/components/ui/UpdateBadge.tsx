import { useEffect, useState } from 'react';
import { Icon } from './Icon';
import { UpdateDialog } from './UpdateDialog';
import { checkUpdate, type UpdateInfo } from '../../lib/host';
import { tr } from '../../lib/i18n';

/** 启动后延迟一会儿再查，别和首屏的数据请求抢；之后每 6 小时查一次。 */
const FIRST_CHECK_MS = 20_000;
const INTERVAL_MS = 6 * 60 * 60 * 1000;

/**
 * 有新版本时在顶栏显示一个向上的箭头，点开是更新说明 + 确认/取消。
 *
 * 这是**自动**提示：查失败时安静地什么都不显示——后台检查失败不该打扰用户。
 * 想主动确认「我是不是最新版」走设置 → 关于（#812），那里会如实区分
 * 「已是最新」与「没查成」。
 *
 * web 端 checkUpdate() 返回 null，整个组件不渲染。
 */
export function UpdateBadge() {
  const [info, setInfo] = useState<UpdateInfo | null>(null);
  const [open, setOpen] = useState(false);

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
      <UpdateDialog info={info} open={open} onClose={() => setOpen(false)} />
    </>
  );
}
