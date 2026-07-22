import { Navigate, useLocation, useParams } from 'react-router-dom';
import { useLibraries, libraryPath } from './hooks';

/**
 * 旧文献追踪路径 `/t/:topicId/wiki` → 该课题隐式库的 `/libraries/{libId}`
 * （保留查询串：?paper= / ?concept= / ?tab= 深链继续可用）。
 * 库列表加载失败或找不到对应库时兜底跳库列表页。
 */
export function TopicWikiRedirect() {
  const { topicId = '' } = useParams();
  const location = useLocation();
  const { data, isLoading } = useLibraries();
  if (isLoading) {
    return (
      <div className="col gap16" style={{ padding: 28 }}>
        <div className="skel" style={{ width: 220, height: 28 }} />
        <div className="skel" style={{ width: '100%', height: 180 }} />
      </div>
    );
  }
  const lib = data?.find((l) => l.project_id === topicId);
  if (!lib) return <Navigate to="/libraries" replace />;
  return <Navigate to={libraryPath(lib.id) + location.search + location.hash} replace />;
}
