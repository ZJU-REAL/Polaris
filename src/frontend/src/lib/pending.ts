import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

/* ============================================================
   按论文 id 记录「进行中」的长任务（下载原文 / AI 编译，都是同步等着的请求）。

   为什么不用组件内的 useMutation：
   - 主从布局里详情面板是同一个组件实例复用的，useMutation 的 isPending 只认
     「最后一次调用」，A 篇在跑、切到 B 篇就会串台；
   - 若靠 key={selectedId} 重挂载来隔离，A 篇的进度又会随卸载一起丢，
     切回来看不到「编译中」。
   所以把状态按论文 id 存在**列表页（父组件）**里：父组件在切换论文时不卸载，
   多篇同时跑也各记各的。
   ============================================================ */

export interface PendingByPaper {
  /** 这篇论文当前是否有任务在跑 */
  has: (id: string) => boolean;
  /** 正在跑的论文数（做全局提示时用得上） */
  size: number;
  /**
   * 包一次异步任务：开始时记上 id，结束（成功或失败）都移除。
   * 同一 id 已在进行中时直接忽略这次调用（返回 undefined），避免重复触发。
   * task 自己负责 toast / invalidate；抛错会原样抛出，调用方通常在 task 内部就地 catch。
   */
  run: <T>(id: string, task: () => Promise<T>) => Promise<T | undefined>;
}

/** 按论文 id 记录进行中的任务（id 是任意字符串，daily 用 entry_id、论文库用 paper_id）。 */
export function usePendingByPaper(): PendingByPaper {
  const [ids, setIds] = useState<ReadonlySet<string>>(() => new Set<string>());
  // 事件里要读「最新值」判重，state 有异步批处理，另存一份同步镜像
  const idsRef = useRef<ReadonlySet<string>>(ids);
  // 组件已卸载后不再 setState（StrictMode 会先卸载再挂载，所以挂载时要重置回 true）
  const aliveRef = useRef(true);
  useEffect(() => {
    aliveRef.current = true;
    return () => {
      aliveRef.current = false;
    };
  }, []);

  const write = useCallback((next: ReadonlySet<string>) => {
    idsRef.current = next;
    if (aliveRef.current) setIds(next);
  }, []);

  const run = useCallback(
    async <T>(id: string, task: () => Promise<T>): Promise<T | undefined> => {
      if (idsRef.current.has(id)) return undefined;
      write(new Set(idsRef.current).add(id));
      try {
        return await task();
      } finally {
        const next = new Set(idsRef.current);
        next.delete(id);
        write(next);
      }
    },
    [write],
  );

  const has = useCallback((id: string) => ids.has(id), [ids]);
  return useMemo(() => ({ has, size: ids.size, run }), [has, ids.size, run]);
}
