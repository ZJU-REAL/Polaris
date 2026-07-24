/* 桌面系统通知。

   这是桌面端最直接的价值：Polaris 的关键事件（等审批的闸门、任务/实验到终态）
   都是小时级异步的，浏览器不开着就收不到。

   实现上不需要主进程配合——页面跑在 app://polaris（secure context），renderer
   里的 Notification API 直接可用。

   两条克制规则：
   1) 只在窗口失焦时补发。用户正看着屏幕时应用内 toast 已经够了，再弹一条系统
      通知是重复打扰。
   2) 只发「需要人介入」与「终态」事件。像 manuscript.ai_writing 那种每个相位
      都推的进度事件绝不能进来，会刷屏。 */

import { isDesktop } from './endpoint';

let permissionAsked = false;

export function notifyDesktop(title: string, body?: string): void {
  if (!isDesktop()) return;
  if (typeof Notification === 'undefined') return;
  if (typeof document !== 'undefined' && document.hasFocus()) return;

  const show = () => {
    try {
      const n = new Notification(title, { body });
      n.onclick = () => {
        window.focus();
        n.close();
      };
    } catch {
      /* 系统层禁用通知等：静默放弃，不影响应用内 toast */
    }
  };

  if (Notification.permission === 'granted') {
    show();
  } else if (Notification.permission === 'default' && !permissionAsked) {
    permissionAsked = true;
    void Notification.requestPermission().then((p) => {
      if (p === 'granted') show();
    });
  }
}
