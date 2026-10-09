import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Modal } from '../../components/ui/Modal';
import { Icon } from '../../components/ui/Icon';
import { toast } from '../../components/ui/Toast';
import { api, type FileVersionMeta, type FileVersionOrigin, type ManuscriptFileMeta } from '../../lib/api';
import { fmtRelative } from '../../lib/format';
import { tr } from '../../lib/i18n';

/* ============================================================
   文件版本历史弹窗：左列版本列表（自动打点：AI 写入前 /
   编译当刻 / 恢复前备份），右侧内容预览 + 恢复到此版本。
   恢复时后端先把当前内容备份成快照，有协同房间时编辑器
   内容会实时更新。
   ============================================================ */

function originText(origin: FileVersionOrigin): string {
  const map: Record<FileVersionOrigin, string> = {
    pre_ai: tr('AI 写入前', 'Before AI edit'),
    compile: tr('编译时', 'At compile'),
    pre_restore: tr('恢复前', 'Before restore'),
  };
  return map[origin] ?? origin;
}

export interface HistoryModalProps {
  open: boolean;
  onClose: () => void;
  manuscriptId: string;
  file: ManuscriptFileMeta;
}

export function HistoryModal({ open, onClose, manuscriptId, file }: HistoryModalProps) {
  const queryClient = useQueryClient();
  const [selectedId, setSelectedId] = useState<string | null>(null);

  const listQuery = useQuery({
    queryKey: ['file-versions', manuscriptId, file.id],
    queryFn: () => api.listFileVersions(manuscriptId, file.id),
    enabled: open,
    retry: false,
  });
  const versions = listQuery.data ?? [];
  const selected = versions.find((v) => v.id === selectedId) ?? versions[0] ?? null;

  const previewQuery = useQuery({
    queryKey: ['file-version', manuscriptId, file.id, selected?.id],
    queryFn: () => api.getFileVersion(manuscriptId, file.id, selected!.id),
    enabled: open && !!selected,
    retry: false,
    staleTime: Infinity,
  });

  const restoreMutation = useMutation({
    mutationFn: (vid: string) => api.restoreFileVersion(manuscriptId, file.id, vid),
    onSuccess: () => {
      toast(tr('已恢复到所选版本', 'Restored this version'), 'ok');
      void queryClient.invalidateQueries({ queryKey: ['file-versions', manuscriptId, file.id] });
      void queryClient.invalidateQueries({ queryKey: ['manuscript-file', manuscriptId, file.id] });
      void queryClient.invalidateQueries({ queryKey: ['manuscript', manuscriptId] });
      onClose();
    },
    onError: (e) => toast(`${tr('恢复失败：', 'Couldn’t restore: ')}${e instanceof Error ? e.message : String(e)}`, 'error'),
  });

  function VersionRow({ v }: { v: FileVersionMeta }) {
    const active = v.id === selected?.id;
    return (
      <div
        className="writer-file"
        onClick={() => setSelectedId(v.id)}
        style={{
          padding: '6px 10px',
          borderRadius: 7,
          cursor: 'pointer',
          background: active ? 'var(--accent-soft)' : undefined,
        }}
      >
        <div className="row gap8">
          <span className="mono" style={{ fontSize: 11, fontWeight: 600, color: active ? 'var(--accent-text)' : 'var(--text-3)' }}>
            #{v.seq}
          </span>
          <span className="pill sm" style={{ height: 16, fontSize: 11, padding: '0 6px' }}>
            {originText(v.origin)}
          </span>
          <span className="mono" style={{ fontSize: 11, color: 'var(--text-3)', marginLeft: 'auto' }}>
            {fmtRelative(v.created_at)}
          </span>
        </div>
        {v.label && (
          <div style={{ fontSize: 11, color: 'var(--text-3)', marginTop: 2, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
            {v.label}
          </div>
        )}
      </div>
    );
  }

  return (
    <Modal
      open={open}
      onClose={onClose}
      width={860}
      title={
        <>
          <Icon name="clock" size={16} style={{ color: 'var(--accent)' }} />
          {tr('版本历史', 'Version history')}
        </>
      }
      sub={file.path}
      footer={
        <>
          <button className="btn btn-ghost sm" onClick={onClose}>
            {tr('关闭', 'Close')}
          </button>
          <button
            className="btn btn-primary sm"
            disabled={!selected || restoreMutation.isPending || file.readonly}
            title={file.readonly ? tr('只读文件不能恢复', 'Read-only files can’t be restored') : tr('当前内容会先保存为一个版本', 'Your current text is saved as a version first')}
            onClick={() => selected && restoreMutation.mutate(selected.id)}
          >
            <Icon name="refresh" size={12} />
            {restoreMutation.isPending ? tr('恢复中…', 'Restoring…') : tr('恢复此版本', 'Restore this version')}
          </button>
        </>
      }
    >
      {listQuery.isLoading ? (
        <div className="empty" style={{ padding: 30 }}>{tr('加载中…', 'Loading…')}</div>
      ) : versions.length === 0 ? (
        <div className="empty" style={{ padding: 30 }}>
          {tr('还没有历史版本。编译或 AI 写入前会自动保存。', 'No versions yet. One is saved each time you compile or before AI edits.')}
        </div>
      ) : (
        <div className="row gap10" style={{ alignItems: 'stretch', height: '52vh' }}>
          <div className="scroll col gap4" style={{ width: 250, flexShrink: 0, overflowY: 'auto' }}>
            {versions.map((v) => (
              <VersionRow key={v.id} v={v} />
            ))}
          </div>
          <div
            className="scroll mono"
            style={{
              flex: 1,
              minWidth: 0,
              overflowY: 'auto',
              border: '0.5px solid var(--border)',
              borderRadius: 8,
              background: 'var(--surface-2)',
              padding: '10px 12px',
              fontSize: 11,
              lineHeight: 1.6,
              whiteSpace: 'pre-wrap',
            }}
          >
            {previewQuery.isLoading ? tr('加载中…', 'Loading…') : previewQuery.data?.content ?? ''}
          </div>
        </div>
      )}
    </Modal>
  );
}
