import { Navigate, useLocation, useParams } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { api } from '../../lib/api';
import { libraryPath } from './hooks';

/**
 * 旧地址 `/t/:topicId/wiki`（课题名下的文献页）→ 现在的去处。应用内部已经不再链接到这里，
 * 只为旧书签和旧任务记录里的链接保留：
 * - 带 `?paper=<id>`：直接打开那篇论文的阅读页（不依赖课题和文献库的对应关系）；
 * - 否则去这个课题关联的文献库：正好一个就进那个库（查询串原样带上），
 *   多个或没有就去文献库列表。
 */
export function TopicWikiRedirect() {
  const { topicId = '' } = useParams();
  const location = useLocation();
  const paperId = new URLSearchParams(location.search).get('paper');
  const { data, isLoading } = useQuery({
    queryKey: ['sourceLibraries', topicId],
    queryFn: () => api.getSourceLibraries(topicId),
    enabled: !!topicId && !paperId,
    retry: false,
  });
  if (paperId) return <Navigate to={`/papers/${paperId}/read`} replace />;
  if (isLoading) {
    return (
      <div className="col gap16" style={{ padding: 28 }}>
        <div className="skel" style={{ width: 220, height: 28 }} />
        <div className="skel" style={{ width: '100%', height: 180 }} />
      </div>
    );
  }
  const only = data && data.length === 1 ? data[0] : undefined;
  if (!only) return <Navigate to="/libraries" replace />;
  return <Navigate to={libraryPath(only.id) + location.search + location.hash} replace />;
}
