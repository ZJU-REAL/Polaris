import { useMemo } from 'react';
import { topicPath, useProject } from '../../app/project';
import type { ProjectRead } from '../../lib/api';
import type { ResearchRunRead } from '../../lib/researchApi';
import { useResearchRuns } from './hooks';
import { waitingFor } from './phase';

/* ============================================================
   课题的统一列表：侧栏、研究首页和任务面板都用这一份。

   一个课题要么有研究进展（从「研究」首页一句方向开始的，有时间线），要么没有
   （之前按老办法建的：只有想法、实验、稿件这些页面）。两种都是课题，都要能在
   「研究」里找到；区别只是点开后先看到时间线还是概览。
   ============================================================ */

export interface Direction {
  /** 课题 id（= project id） */
  projectId: string;
  name: string;
  /** 有研究进展时是那次研究；没有为 null */
  run: ResearchRunRead | null;
  /** 点开去哪：有研究进展去时间线，没有去课题概览 */
  href: string;
  updatedAt: string;
}

/** 等你处理的排最前，其次是运行中的，再是其余；同组内按最近更新。 */
function rank(d: Direction): number {
  if (!d.run) return 2;
  if (waitingFor(d.run)) return 0;
  return d.run.status === 'running' ? 1 : 2;
}

export function mergeDirections(projects: ProjectRead[], runs: ResearchRunRead[]): Direction[] {
  const byProject = new Map<string, ResearchRunRead>();
  for (const run of runs) {
    // 同一个课题理论上只有一次研究；万一有多条，取最近更新的那条
    const prev = byProject.get(run.project_id);
    if (!prev || run.updated_at > prev.updated_at) byProject.set(run.project_id, run);
  }
  const out: Direction[] = [];
  const seen = new Set<string>();
  for (const run of byProject.values()) {
    seen.add(run.project_id);
    out.push({ projectId: run.project_id, name: run.name, run, href: `/research/${run.id}`, updatedAt: run.updated_at });
  }
  for (const p of projects) {
    if (seen.has(p.id)) continue;
    out.push({ projectId: p.id, name: p.name, run: null, href: topicPath(p.id), updatedAt: p.updated_at });
  }
  return out.sort((a, b) => rank(a) - rank(b) || b.updatedAt.localeCompare(a.updatedAt));
}

export interface DirectionsValue {
  directions: Direction[];
  isLoading: boolean;
  /** 研究列表取不到（引擎连不上等）；老课题那一半可能还在 */
  runsError: unknown;
  refetch: () => void;
  /** 按课题 id 找 */
  byProject: (projectId: string | null | undefined) => Direction | null;
  /** 文献库属于哪个课题的研究（任务详情页用它决定「返回」去哪） */
  byLibrary: (libraryId: string | null | undefined) => Direction | null;
}

export function useDirections(): DirectionsValue {
  const { projects, isLoading: projectsLoading } = useProject();
  const runsQuery = useResearchRuns();
  const runs = runsQuery.data;
  const directions = useMemo(() => mergeDirections(projects, runs ?? []), [projects, runs]);
  return useMemo(
    () => ({
      directions,
      isLoading: projectsLoading || runsQuery.isLoading,
      runsError: runsQuery.isError && !runs ? runsQuery.error : null,
      refetch: () => void runsQuery.refetch(),
      byProject: (projectId) => (projectId ? (directions.find((d) => d.projectId === projectId) ?? null) : null),
      byLibrary: (libraryId) =>
        libraryId ? (directions.find((d) => d.run?.library_id === libraryId) ?? null) : null,
    }),
    [directions, projectsLoading, runsQuery, runs],
  );
}
