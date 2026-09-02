import { useQuery } from '@tanstack/react-query';
import { useNavigate } from 'react-router-dom';
import { EmptyState } from '../../components/ui/EmptyState';
import { Icon } from '../../components/ui/Icon';
import { Modal } from '../../components/ui/Modal';
import { api, type DownloadBatchRead } from '../../lib/api';
import { countDownloadBatchItems } from '../../lib/extension-download-batches';
import { fmtTime } from '../../lib/format';
import { tr } from '../../lib/i18n';

const STATUS_LABELS: Record<string, [string, string]> = {
  queued: ['等待扩展', 'Waiting for extension'],
  running: ['处理中', 'In progress'],
  completed: ['已完成', 'Completed'],
  partial: ['部分完成', 'Partially completed'],
  failed: ['失败', 'Failed'],
};

function BatchRow({ batch }: { batch: DownloadBatchRead }) {
  const counts = countDownloadBatchItems(batch);
  return (
    <div style={{ padding: '13px 2px', borderBottom: '1px solid var(--border)' }}>
      <div className="row gap8 wrap" style={{ justifyContent: 'space-between' }}>
        <span className="row gap8 wrap">
          <strong style={{ fontSize: 12.5 }}>{tr(...(STATUS_LABELS[batch.status] ?? [batch.status, batch.status]))}</strong>
          <span className="mono" style={{ fontSize: 10.5, color: 'var(--text-4)' }}>{batch.id.slice(0, 8)}</span>
        </span>
        <time className="mono" style={{ fontSize: 10.5, color: 'var(--text-3)' }}>{fmtTime(batch.created_at)}</time>
      </div>
      <div className="row gap6 wrap" style={{ marginTop: 9 }}>
        <span className="pill sm">{tr(`共 ${counts.total} 篇`, `${counts.total} papers`)}</span>
        {!!counts.active && <span className="pill sm">{tr(`处理中 ${counts.active}`, `${counts.active} active`)}</span>}
        {!!counts.cached && <span className="pill sm" style={{ color: 'var(--ok-tx)' }}>{tr(`已有 PDF ${counts.cached}`, `${counts.cached} cached`)}</span>}
        {!!counts.uploaded && <span className="pill sm" style={{ color: 'var(--ok-tx)' }}>{tr(`已归档 ${counts.uploaded}`, `${counts.uploaded} archived`)}</span>}
        {!!counts.failed && <span className="pill sm" style={{ color: 'var(--danger-tx)' }}>{tr(`需处理 ${counts.failed}`, `${counts.failed} need attention`)}</span>}
      </div>
      {!!counts.failed && (
        <p style={{ margin: '8px 0 0', color: 'var(--text-3)', fontSize: 11.5, lineHeight: 1.55 }}>
          {tr('打开 Polaris 扩展查看失败原因并重试对应论文。', 'Open the Polaris extension to inspect and retry the affected papers.')}
        </p>
      )}
    </div>
  );
}

export function ExtensionBatchHistoryModal({
  libraryId,
  open,
  onClose,
}: {
  libraryId: string;
  open: boolean;
  onClose: () => void;
}) {
  const navigate = useNavigate();
  const batchesQuery = useQuery({
    queryKey: ['download-batches', libraryId],
    queryFn: () => api.listDownloadBatches(libraryId),
    enabled: open,
    retry: false,
    refetchInterval: open ? 5_000 : false,
  });

  return (
    <Modal
      open={open}
      onClose={onClose}
      title={tr('最近扩展任务', 'Recent extension batches')}
      sub={tr('一个任务可包含多篇论文，每篇保持独立归档绑定。', 'One batch can contain multiple independently bound papers.')}
      width={680}
      footer={(
        <div className="row gap8" style={{ justifyContent: 'space-between', width: '100%' }}>
          <button className="btn btn-ghost sm" onClick={() => { onClose(); navigate('/settings?tab=extension'); }}>
            <Icon name="settings" size={13} />
            {tr('扩展连接设置', 'Extension settings')}
          </button>
          <button className="btn btn-primary sm" onClick={onClose}>{tr('完成', 'Done')}</button>
        </div>
      )}
    >
      {batchesQuery.isLoading ? (
        <div style={{ display: 'grid', gap: 10 }}>
          <div className="skel" style={{ height: 76 }} />
          <div className="skel" style={{ height: 76 }} />
        </div>
      ) : batchesQuery.isError ? (
        <EmptyState
          compact
          icon="x"
          title={tr('无法加载扩展任务', 'Could not load extension batches')}
          action={<button className="btn btn-soft sm" onClick={() => void batchesQuery.refetch()}>{tr('重试', 'Retry')}</button>}
        />
      ) : !batchesQuery.data?.length ? (
        <EmptyState compact icon="download" title={tr('还没有扩展任务', 'No extension batches yet')} />
      ) : (
        <div>{batchesQuery.data.map((batch) => <BatchRow batch={batch} key={batch.id} />)}</div>
      )}
    </Modal>
  );
}
