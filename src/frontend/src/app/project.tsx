import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from 'react';
import { useQuery } from '@tanstack/react-query';
import { api, type ProjectRead } from '../lib/api';

/* ============================================================
   当前研究方向（project）上下文：
   - GET /projects 列表（TanStack Query）
   - 当前选中项目 id 持久化 localStorage，供全站使用
   ============================================================ */

const CURRENT_KEY = 'polaris.project';

export interface ProjectContextValue {
  /** 本人是成员的项目列表（后端不可用时为 []）。 */
  projects: ProjectRead[];
  isLoading: boolean;
  /** 列表加载失败（后端未起/404 等），UI 需优雅降级。 */
  isError: boolean;
  currentProjectId: string | null;
  currentProject: ProjectRead | null;
  setCurrentProjectId: (id: string | null) => void;
}

const ProjectContext = createContext<ProjectContextValue | null>(null);

export function ProjectProvider({ children }: { children: ReactNode }) {
  const [currentProjectId, setIdState] = useState<string | null>(() => localStorage.getItem(CURRENT_KEY));

  const { data, isLoading, isError } = useQuery({
    queryKey: ['projects'],
    queryFn: () => api.listProjects(),
    retry: false,
    staleTime: 30_000,
  });
  const projects = useMemo(() => data ?? [], [data]);

  const setCurrentProjectId = useCallback((id: string | null) => {
    setIdState(id);
    if (id) localStorage.setItem(CURRENT_KEY, id);
    else localStorage.removeItem(CURRENT_KEY);
  }, []);

  // 列表加载后：若当前 id 失效则回退到第一个项目
  useEffect(() => {
    if (!data) return;
    if (data.length === 0) return;
    if (!currentProjectId || !data.some((p) => p.id === currentProjectId)) {
      const first = data[0];
      if (first) setCurrentProjectId(first.id);
    }
  }, [data, currentProjectId, setCurrentProjectId]);

  const currentProject = useMemo(
    () => projects.find((p) => p.id === currentProjectId) ?? null,
    [projects, currentProjectId],
  );

  const value = useMemo<ProjectContextValue>(
    () => ({ projects, isLoading, isError, currentProjectId, currentProject, setCurrentProjectId }),
    [projects, isLoading, isError, currentProjectId, currentProject, setCurrentProjectId],
  );

  return <ProjectContext.Provider value={value}>{children}</ProjectContext.Provider>;
}

export function useProject(): ProjectContextValue {
  const ctx = useContext(ProjectContext);
  if (!ctx) throw new Error('useProject must be used within <ProjectProvider>');
  return ctx;
}
