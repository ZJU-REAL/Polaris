import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Modal } from '../../components/ui/Modal';
import { Icon } from '../../components/ui/Icon';
import { toast } from '../../components/ui/Toast';
import { api, type FileVersionMeta, type FileVersionOrigin, type ManuscriptFileMeta } from '../../lib/api';
import { fmtRelative } from '../../lib/format';

/* ============================================================
   文件版本历史弹窗：左列版本列表（自动打点：AI 写入前 /
   编译当刻 / 恢复前备份），右侧内容预览 + 「恢复到此版本」。
   恢复时后端先把当前内容备份成快照，有协同房间时编辑器
   内容会实时更新。
   ============================================================ */

const ORIGIN_TEXT: Record<FileVersionOrigin, string> = {
  pre_ai: 'AI 写入前',
  compile: '编译',
  pre_restore: '恢复前备份',
};

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
      toast('已恢复到所选版本（恢复前内容已自动备份）', 'ok');
      void queryClient.invalidateQueries({ queryKey: ['file-versions', manuscriptId, file.id] });
      void queryClient.invalidateQueries({ queryKey: ['manuscript-file', manuscriptId, file.id] });
      void queryClient.invalidateQueries({ queryKey: ['manuscript', manuscriptId] });
      onClose();
    },
    onError: (e) => toast(`恢复失败：${e instanceof Error ? e.message : String(e)}`, 'error'),
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
          <span className="mono" style={{ fontSize: 10.5, fontWeight: 700, color: active ? 'var(--accent-text)' : 'var(--text-3)' }}>
            #{v.seq}
          </span>
          <span className="pill sm" style={{ height: 16, fontSize: 9.5, padding: '0 6px' }}>
            {ORIGIN_TEXT[v.origin] ?? v.origin}
          </span>
          <span className="mono" style={{ fontSize: 10, color: 'var(--text-4)', marginLeft: 'auto' }}>
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
          版本历史
        </>
      }
      sub={`${file.path} · AI 写入前与每次编译自动存档，最多保留 50 份`}
      footer={
        <>
          <button className="btn btn-ghost sm" onClick={onClose}>
            关闭
          </button>
          <button
            className="btn btn-primary sm"
            disabled={!selected || restoreMutation.isPending || file.readonly}
            title={file.readonly ? '只读文件不能恢复' : '恢复前会自动备份当前内容'}
            onClick={() => selected && restoreMutation.mutate(selected.id)}
          >
            <Icon name="refresh" size={12} />
            {restoreMutation.isPending ? '恢复中…' : '恢复到此版本'}
          </button>
        </>
      }
    >
      {listQuery.isLoading ? (
        <div className="empty" style={{ padding: 30 }}>加载版本列表…</div>
      ) : versions.length === 0 ? (
        <div className="empty" style={{ padding: 30 }}>
          还没有版本存档。AI 起草写入前和每次编译时会自动存档。
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
            {previewQuery.isLoading ? '加载内容…' : previewQuery.data?.content ?? ''}
          </div>
        </div>
      )}
    </Modal>
  );
}
