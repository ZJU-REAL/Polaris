import { useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Icon } from '../../components/ui/Icon';
import { PageHead } from '../../components/ui/PageHead';
import { EmptyState } from '../../components/ui/EmptyState';
import { toast } from '../../components/ui/Toast';
import { api, isAdmin, type DirectionLibraryDetail } from '../../lib/api';
import { tr } from '../../lib/i18n';
import { WikiWorkbench } from '../wiki/WikiPage';
import { LibraryBrowse } from './LibraryBrowse';

/* ============================================================
   /libraries/:id — 文献库详情（P5c + P6 治理 + P9b 生命周期）
   - 可管理者（成员 / 文献库管理员 / 创建者 / 平台管理员）：完整工作台
     （论文/概念/图谱/对话/建库/笔记/治理）。有起源课题的库走 project 作用域端点；
     独立库（project_id=NULL）同一套工作台改走 /libraries 端点。
   - 其他人：干净的只读浏览（论文 + 概念）。
   顶部按状态显示横幅：待审批 / 已驳回；平台管理员可就地批准 / 驳回。
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
    <div className="page fadeup" style={{ maxWidth: 1360, display: 'flex', flexDirection: 'column', height: '100%', paddingBottom: 24 }}>
      <PageHead
        eyebrow={tr('实验室 · 文献库', 'Lab · Library')}
        title={`${tr('文献库', 'Library')} · ${lib.name}`}
        dense
        sub={lib.statement ?? undefined}
        right={
          <button className="btn btn-ghost" onClick={() => navigate('/libraries')}>
            <Icon name="chevron" size={13} style={{ transform: 'rotate(180deg)' }} />
            {tr('全部文献库', 'All libraries')}
          </button>
        }
      />
      <StatusBanner lib={lib} />
      {canManage ? (
        <WikiWorkbench pid={lib.project_id ?? undefined} libraryId={lib.id} />
      ) : (
        <LibraryBrowse libraryId={lib.id} />
      )}
    </div>
  );
}

/* —— 状态横幅：待审批 / 已驳回；平台管理员就地批准 / 驳回 —— */

function StatusBanner({ lib }: { lib: DirectionLibraryDetail }) {
  const queryClient = useQueryClient();
  const { data: me } = useQuery({ queryKey: ['me'], queryFn: () => api.me(), retry: false, staleTime: 60_000 });
  const admin = isAdmin(me);
  const [rejecting, setRejecting] = useState(false);
  const [note, setNote] = useState('');

  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: ['library', lib.id] });
    void queryClient.invalidateQueries({ queryKey: ['libraries'] });
  };
  const approve = useMutation({
    mutationFn: () => api.approveLibrary(lib.id),
    onSuccess: () => { toast(tr('已激活，可以开始抓取了', 'Activated — ingest can start now'), 'ok'); invalidate(); },
    onError: () => toast(tr('操作失败，请重试', 'Action failed, please retry'), 'error'),
  });
  const reject = useMutation({
    mutationFn: () => api.rejectLibrary(lib.id, note.trim() || null),
    onSuccess: () => { toast(tr('已驳回', 'Rejected'), 'ok'); setRejecting(false); setNote(''); invalidate(); },
    onError: () => toast(tr('操作失败，请重试', 'Action failed, please retry'), 'error'),
  });

  if (lib.status === 'active') return null;

  const pending = lib.status === 'pending';
  const bg = pending ? 'var(--warn-bg)' : 'var(--danger-bg)';
  const tx = pending ? 'var(--warn-tx)' : 'var(--danger-tx)';

  return (
    <div
      className="card"
      style={{ background: bg, borderColor: tx, padding: '12px 16px', marginBottom: 14 }}
    >
      <div className="row" style={{ justifyContent: 'space-between', gap: 12, flexWrap: 'wrap' }}>
        <div className="col gap4" style={{ minWidth: 0 }}>
          <div className="row gap8" style={{ color: tx, fontWeight: 680, fontSize: 13.5 }}>
            <Icon name={pending ? 'clock' : 'x'} size={14} />
            {pending ? tr('待审批', 'Pending review') : tr('已驳回', 'Rejected')}
          </div>
          <div style={{ fontSize: 12.5, color: 'var(--text-3)' }}>
            {pending
              ? tr('管理员激活后才能开始抓取；创建本身不花额度。', 'Ingest can start only after an admin activates it; creating costs nothing.')
              : lib.review_note
                ? tr(`驳回理由：${lib.review_note}`, `Reason: ${lib.review_note}`)
                : tr('未通过审批。可调整配置后请管理员重新审批。', 'Not approved. Adjust the config and ask an admin to review again.')}
          </div>
        </div>
        {admin && (
          <div className="row gap8" style={{ flexShrink: 0 }}>
            <button className="btn btn-primary sm" disabled={approve.isPending} onClick={() => approve.mutate()}>
              {approve.isPending ? tr('处理中…', 'Working…') : tr('批准激活', 'Approve')}
            </button>
            {pending && (
              <button className="btn btn-soft sm" disabled={reject.isPending} onClick={() => setRejecting((v) => !v)}>
                {tr('驳回', 'Reject')}
              </button>
            )}
          </div>
        )}
      </div>
      {admin && rejecting && (
        <div className="row gap8" style={{ marginTop: 10 }}>
          <input
            className="input"
            style={{ flex: 1 }}
            placeholder={tr('驳回理由（可选）', 'Reason (optional)')}
            value={note}
            onChange={(e) => setNote(e.target.value)}
          />
          <button className="btn btn-primary sm" disabled={reject.isPending} onClick={() => reject.mutate()}>
            {tr('确认驳回', 'Confirm reject')}
          </button>
          <button className="btn btn-ghost sm" onClick={() => { setRejecting(false); setNote(''); }}>
            {tr('取消', 'Cancel')}
          </button>
        </div>
      )}
    </div>
  );
}
