import { useQuery } from '@tanstack/react-query';
import { api, type DirectionLibrarySummary } from '../../lib/api';

/* ============================================================
   共享方向库（P5c）数据钩子与路径工具。
   ============================================================ */

export interface LibraryFilters {
  type?: 'personal' | 'public' | 'all';
  status?: DirectionLibrarySummary['status'];
}

/** 全部方向库列表（全实验室可读；列表小、5 分钟内不重拉）。可按归属类型/状态过滤。 */
export function useLibraries(filters: LibraryFilters = {}, enabled = true) {
  const type = filters.type ?? 'all';
  const status = filters.status ?? null;
  return useQuery({
    queryKey: ['libraries', type, status],
    queryFn: () => api.listLibraries({ type, ...(status ? { status } : {}) }),
    staleTime: 300_000,
    retry: false,
    enabled,
  });
}

/** 课题 → 其隐式方向库（旧 /t/:id/wiki 链接改写用）；列表未就绪时返回 null。 */
export function useTopicLibrary(topicId: string | null | undefined): DirectionLibrarySummary | null {
  const { data } = useLibraries({}, !!topicId);
  if (!topicId) return null;
  return data?.find((l) => l.project_id === topicId) ?? null;
}

/** 库详情路径：`/libraries/<id>`（suffix 可带查询串，如 `?tab=ingest`）。 */
export function libraryPath(libraryId: string, suffix = ''): string {
  return `/libraries/${libraryId}${suffix}`;
}
