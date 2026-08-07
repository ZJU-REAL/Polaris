import { api } from './api';

/* ============================================================
   浏览打点。

   **尽力而为**：失败一律咽掉。统计是附带产物，不能因为它让页面出错或变慢——
   一个因为埋点挂了而打不开的论文页，比没有热度榜糟得多。

   去重在后端做（同一个人一小时内只算一次）。前端只在这里挡住 React 严格模式下
   同一次挂载的重复调用，省一个来回。
   ============================================================ */

const seen = new Set<string>();

export function trackView(kind: 'library' | 'paper', targetId: string | null | undefined): void {
  if (!targetId) return;
  const key = `${kind}:${targetId}`;
  if (seen.has(key)) return;
  seen.add(key);
  void api.recordView(kind, targetId).catch(() => {
    // 记不上就算了。下次打开还有机会，而这次阅读本身不该受影响。
    seen.delete(key);
  });
}
