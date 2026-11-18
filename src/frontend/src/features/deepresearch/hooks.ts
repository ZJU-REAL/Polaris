import { useEffect, useState } from 'react';
import { keepPreviousData, useQuery } from '@tanstack/react-query';
import {
  isEngineUnreachable,
  researchApi,
  type Capabilities,
  type ResearchEstimate,
  type ResearchRunDetail,
  type ResearchRunRead,
} from '../../lib/researchApi';
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
  capabilities: ['capabilities'] as const,
  estimate: ['research-estimate'] as const,
};

/**
 * 这台电脑能不能编译 LaTeX、有没有登记过远程机器。不依赖研究：一次研究都没有的课题
 * 也要知道。还没取到（或旧引擎没有这个接口）时 data 是 undefined，调用方当「不知道」处理。
 */
export function useCapabilities(enabled = true) {
  return useQuery<Capabilities>({
    queryKey: researchKeys.capabilities,
    queryFn: () => researchApi.getCapabilities(),
    enabled,
    retry: false,
    staleTime: 30_000,
  });
}

/** 一次研究通常要多久：实际跑下来是 38–49 分钟，所以没有估计接口时说 30–50 分钟。 */
export const STATIC_ESTIMATE: Required<Pick<ResearchEstimate, 'minutes_low' | 'minutes_high'>> = {
  minutes_low: 30,
  minutes_high: 50,
};

/** 开始前的估计。接口不存在（404）或失败时返回固定范围，不报错。 */
export function useResearchEstimate(enabled = true): ResearchEstimate {
  const query = useQuery<ResearchEstimate>({
    queryKey: researchKeys.estimate,
    queryFn: () => researchApi.getEstimate(),
    enabled,
    retry: false,
    staleTime: 10 * 60_000,
  });
  const d = query.data;
  const lo = typeof d?.minutes_low === 'number' && d.minutes_low > 0 ? d.minutes_low : null;
  const hi = typeof d?.minutes_high === 'number' && d.minutes_high > 0 ? d.minutes_high : null;
  if (lo === null || hi === null || hi < lo) return { ...STATIC_ESTIMATE };
  return { ...d, minutes_low: lo, minutes_high: hi };
}

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
