import { useEffect, useState } from 'react';
import { keepPreviousData, useQuery } from '@tanstack/react-query';
import { isEngineUnreachable, researchApi, type ResearchRunDetail, type ResearchRunRead } from '../../lib/researchApi';
import { pollInterval } from './phase';

/* ============================================================
   研究的两份服务端状态：列表与单次研究。

   轮询是事实来源：运行中 5 秒、等用户 30 秒、其余不轮询；任务事件流只是让下一次
   取数来得更早（见 ResearchRunPage）。引擎连不上时保留上一次的数据并继续每 5 秒
   重试——页面只多一条提示，不清空、不跳错误页，连上后自己恢复。
   ============================================================ */

const RETRY_WHEN_DOWN_MS = 5000;

export const researchKeys = {
  list: ['research-runs'] as const,
  run: (id: string) => ['research', id] as const,
};

export function useResearchRuns() {
  return useQuery<ResearchRunRead[]>({
    queryKey: researchKeys.list,
    queryFn: () => researchApi.listRuns(),
    retry: false,
    placeholderData: keepPreviousData,
    refetchInterval: (query) => {
      if (query.state.status === 'error') return RETRY_WHEN_DOWN_MS;
      const runs = query.state.data ?? [];
      if (runs.some((r) => r.status === 'running')) return 5000;
      if (runs.some((r) => r.status === 'waiting_user')) return 30000;
      return false;
    },
  });
}

export function useResearchRun(id: string) {
  return useQuery<ResearchRunDetail>({
    queryKey: researchKeys.run(id),
    queryFn: () => researchApi.getRun(id),
    enabled: !!id,
    retry: false,
    placeholderData: keepPreviousData,
    refetchInterval: (query) => {
      if (query.state.status === 'error') {
        // 找不到 / 被拒绝这类明确的回答不必反复问；连不上才一直重试
        return isEngineUnreachable(query.state.error) ? RETRY_WHEN_DOWN_MS : false;
      }
      return pollInterval(query.state.data?.status);
    },
  });
}

/** 每隔一段时间重渲染一次，给「约 4 分钟后重试」这类随时间变的文字用。 */
export function useNow(everyMs: number, active = true): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!active) return;
    setNow(Date.now());
    const t = setInterval(() => setNow(Date.now()), everyMs);
    return () => clearInterval(t);
  }, [everyMs, active]);
  return now;
}
