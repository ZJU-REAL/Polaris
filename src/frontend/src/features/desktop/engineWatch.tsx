/* ============================================================
   引擎中途退出时界面怎么办。

   主进程看着引擎进程：退出或卡死就自动重启一次，并把状态变化推过来
   （engine.state 事件）。以前没有这层：引擎一挂，页面上只剩一处处零散的
   「请求失败」，直到用户自己想到去重启应用。

   - restarting：盖一层说明，不重新加载页面——引擎几秒后回来，会话口令不变，
     用户在页面上填了一半的东西都还在。
   - running：撤掉说明。请求恢复正常；顺手让数据层把正在显示的内容重新取一遍。
   - failed：重新加载。启动流程会读到失败状态，显示带原因和日志入口的兜底页。

   这里自己挂一个独立的 React 根，而不是插进 App 的组件树：它要盖在任何页面
   之上，并且不依赖应用内的路由与数据层还活着。
   ============================================================ */

import { createRoot, type Root } from 'react-dom/client';
import { PolarisMark } from '../../components/ui/PolarisLogo';
import { onHostEvent, type HostEvent } from '../../lib/host';
import { tr, useLang } from '../../lib/i18n';

export type WatchAction = 'show' | 'hide' | 'reload' | 'ignore';

/** 事件 → 界面动作（纯函数，单测直测）。 */
export function engineWatchAction(event: HostEvent, overlayShown: boolean): WatchAction {
  if (event.type !== 'engine.state') return 'ignore';
  if (event.state === 'restarting') return overlayShown ? 'ignore' : 'show';
  if (event.state === 'running') return overlayShown ? 'hide' : 'ignore';
  return 'reload';
}

export function EngineRestartingOverlay() {
  useLang();
  return (
    <div
      role="alertdialog"
      aria-modal="true"
      aria-label={tr('本机引擎已停止', 'The local engine stopped')}
      data-testid="engine-restarting-overlay"
      style={{
        position: 'fixed',
        inset: 0,
        zIndex: 2147483000,
        display: 'grid',
        placeItems: 'center',
        background: 'rgba(15, 23, 42, 0.32)',
        backdropFilter: 'blur(2px)',
      }}
    >
      <div
        style={{
          width: 'min(360px, calc(100vw - 48px))',
          padding: '26px 28px 24px',
          borderRadius: 'var(--radius-lg)',
          background: 'var(--surface, #fff)',
          border: '0.5px solid var(--border)',
          boxShadow: '0 18px 50px rgba(23, 58, 110, 0.18)',
          textAlign: 'center',
        }}
      >
        <PolarisMark size={36} />
        <div style={{ marginTop: 12, fontSize: 15, fontWeight: 600, color: 'var(--text-1)' }}>
          {tr('本机引擎已停止', 'The local engine stopped')}
        </div>
        <div style={{ marginTop: 6, fontSize: 13, color: 'var(--text-3)' }}>
          {tr('正在重新启动，通常只需要几秒…', 'Restarting it. This usually takes a few seconds…')}
        </div>
      </div>
    </div>
  );
}

let started = false;
let container: HTMLElement | null = null;
let root: Root | null = null;

function show(): void {
  if (root) return;
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
  root.render(<EngineRestartingOverlay />);
  // 数据层（TanStack Query）听这一对事件：离线时暂停请求而不是一个个报错，
  // 恢复时把正在显示的查询重新取一遍。引擎就是这个应用的「网络」。
  window.dispatchEvent(new Event('offline'));
}

function hide(): void {
  root?.unmount();
  container?.remove();
  root = null;
  container = null;
  window.dispatchEvent(new Event('online'));
}

/** 开始看着引擎状态。幂等；没有桌面宿主（浏览器开发态）时是 no-op。 */
export function startEngineWatch(): void {
  if (started || typeof window === 'undefined') return;
  started = true;
  onHostEvent((event) => {
    const action = engineWatchAction(event, root !== null);
    if (action === 'show') show();
    else if (action === 'hide') hide();
    else if (action === 'reload') window.location.reload();
  });
}
