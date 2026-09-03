import { useNavigate, useParams } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { EmptyState } from '../../components/ui/EmptyState';
import { api } from '../../lib/api';
import { tr } from '../../lib/i18n';
import { WikiWorkbench } from '../wiki/WikiPage';
import { LibraryBrowse } from './LibraryBrowse';

/* ============================================================
   /libraries/:id — 文献库详情（P5c + P6 治理）
   - 可管理者（成员 / 创建者 / 平台管理员）：完整工作台
     （论文/概念/图谱/对话/建库/笔记/治理）。有起源课题的库走 project 作用域端点；
     独立库（project_id=NULL）同一套工作台改走 /libraries 端点。
   - 其他人：干净的只读浏览（论文 + 概念）。
   ============================================================ */

export function LibraryDetailPage() {
  const { id = '' } = useParams();
  const navigate = useNavigate();

  const { data: lib, isLoading, isError, refetch } = useQuery({
    queryKey: ['library', id],
    queryFn: () => api.getLibrary(id),
    enabled: !!id,
    retry: false,
  });

  if (isLoading) {
    return (
      <div className="page fadeup" style={{ maxWidth: 1360 }}>
        <div className="col gap16">
          <div className="skel" style={{ width: 260, height: 30 }} />
          <div className="skel" style={{ width: '50%', height: 14 }} />
          <div className="skel" style={{ width: '100%', height: 320 }} />
        </div>
      </div>
    );
  }
  if (isError || !lib) {
    return (
      <div className="page fadeup" style={{ maxWidth: 1360 }}>
        <EmptyState
          icon="x"
          title={tr('打不开这个文献库', 'Cannot open this library')}
          desc={tr('文献库不存在，或后端暂时不可用。', 'It does not exist, or the backend is unavailable.')}
          action={
            <div className="row gap10">
              <button className="btn btn-soft sm" onClick={() => void refetch()}>
                {tr('重试', 'Retry')}
              </button>
              <button className="btn btn-ghost sm" onClick={() => navigate('/libraries')}>
                {tr('回文献库列表', 'Back to libraries')}
              </button>
            </div>
          }
        />
      </div>
    );
  }

  const canManage = lib.can_manage;

  return (
    <div className="page fadeup page-fill" style={{ maxWidth: 1360, paddingBottom: 24 }}>
      {/* 库名与返回入口都在顶栏面包屑里（实验室 › 文献库 › 库名），页面不再单占一行 */}
      {canManage ? (
        <WikiWorkbench
          pid={lib.project_id ?? undefined}
          libraryId={lib.id}
          canManage={canManage}
          canManageDiscovery={canManage}
        />
      ) : (
        <LibraryBrowse libraryId={lib.id} />
      )}
    </div>
  );
}
