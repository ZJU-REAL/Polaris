import { useSyncExternalStore } from 'react';

/* ============================================================
   前端本地偏好（存 localStorage，设置页可切换）。
   - 目前只有「任务终端展示历史日志」一项，后续 UI 偏好可同法扩展。
   - 用 useSyncExternalStore 让设置页开关与其它页面即时同步。
   ============================================================ */

const TASK_LOG_HISTORY_KEY = 'polaris.taskLogHistory';

function readTaskLogHistory(): boolean {
  try {
    // 默认开：只有显式存过 '0' 才视为关闭。
    return localStorage.getItem(TASK_LOG_HISTORY_KEY) !== '0';
  } catch {
    return true;
  }
}

let taskLogHistory = readTaskLogHistory();
const listeners = new Set<() => void>();

export function getTaskLogHistory(): boolean {
  return taskLogHistory;
}

export function setTaskLogHistory(on: boolean): void {
  if (on === taskLogHistory) return;
  taskLogHistory = on;
  try {
    localStorage.setItem(TASK_LOG_HISTORY_KEY, on ? '1' : '0');
  } catch {
    /* 隐私模式等写不进去：仅本次会话生效 */
  }
  listeners.forEach((fn) => fn());
}

function subscribe(fn: () => void): () => void {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

/** 响应式读「任务终端展示历史日志」偏好。 */
export function useTaskLogHistory(): boolean {
  return useSyncExternalStore(subscribe, getTaskLogHistory);
}
