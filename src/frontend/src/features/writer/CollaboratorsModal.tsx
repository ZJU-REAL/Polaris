import { useEffect, useMemo, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Modal } from '../../components/ui/Modal';
import { Icon } from '../../components/ui/Icon';
import { toast } from '../../components/ui/Toast';
import { api, ApiError, type CollaboratorRead } from '../../lib/api';
import { tr } from '../../lib/i18n';

/* ============================================================
   协作者管理弹窗：列出协作者（owner 高亮），搜索平台用户加入，
   移除非 owner，生成协同编辑分享链接并复制。
   加人/移除/生成链接需研究方向 owner 或管理员权限（403 提示）。
   ============================================================ */

export interface CollaboratorsModalProps {
  open: boolean;
  onClose: () => void;
  manuscriptId: string;
}

/** 403（无权限）时统一提示。 */
function isForbidden(e: unknown): boolean {
  return e instanceof ApiError && e.status === 403;
}

function initials(name: string): string {
  return (name.trim()[0] ?? '?').toUpperCase();
}

export function CollaboratorsModal({ open, onClose, manuscriptId }: CollaboratorsModalProps) {
  const queryClient = useQueryClient();
  const [term, setTerm] = useState('');
  const [debounced, setDebounced] = useState('');
  const [shareUrl, setShareUrl] = useState<string | null>(null);

  useEffect(() => {
    if (!open) {
      setTerm('');
      setDebounced('');
      setShareUrl(null);
    }
  }, [open]);

  // 搜索输入防抖 250ms
  useEffect(() => {
    const t = setTimeout(() => setDebounced(term.trim()), 250);
    return () => clearTimeout(t);
  }, [term]);

  const listQuery = useQuery({
    queryKey: ['collaborators', manuscriptId],
    queryFn: () => api.listCollaborators(manuscriptId),
    enabled: open,
    retry: false,
  });
  const collaborators = useMemo(() => listQuery.data ?? [], [listQuery.data]);
  const existingIds = useMemo(() => new Set(collaborators.map((c) => c.user_id)), [collaborators]);

  const searchQuery = useQuery({
    queryKey: ['user-search', debounced],
    queryFn: () => api.searchUsers(debounced),
    enabled: open && debounced.length > 0,
    retry: false,
    staleTime: 30_000,
  });
  const results = (searchQuery.data ?? []).filter((u) => !existingIds.has(u.id));

  const addMutation = useMutation({
    mutationFn: (userId: string) => api.addCollaborator(manuscriptId, userId),
    onSuccess: (rows) => {
      queryClient.setQueryData(['collaborators', manuscriptId], rows);
      setTerm('');
      setDebounced('');
      toast(tr('已添加协作者', 'Collaborator added'), 'ok');
    },
    onError: (e) => {
      if (isForbidden(e)) toast(tr('需要研究方向 owner 或管理员权限', 'Requires research-direction owner or admin permission'), 'error');
      else toast(`${tr('添加失败：', 'Add failed: ')}${e instanceof Error ? e.message : String(e)}`, 'error');
    },
  });

  const removeMutation = useMutation({
    mutationFn: (userId: string) => api.removeCollaborator(manuscriptId, userId),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['collaborators', manuscriptId] });
      toast(tr('已移除协作者', 'Collaborator removed'), 'ok');
    },
    onError: (e) => {
      if (isForbidden(e)) toast(tr('需要研究方向 owner 或管理员权限', 'Requires research-direction owner or admin permission'), 'error');
      else if (e instanceof ApiError && e.status === 409) toast(tr('不能移除 owner', 'Cannot remove the owner'), 'error');
      else toast(`${tr('移除失败：', 'Remove failed: ')}${e instanceof Error ? e.message : String(e)}`, 'error');
    },
  });

  const shareMutation = useMutation({
    mutationFn: () => api.createManuscriptShareLink(manuscriptId),
    onSuccess: (link) => {
      setShareUrl(`${window.location.origin}${link.join_path}`);
    },
    onError: (e) => {
      if (isForbidden(e)) toast(tr('需要研究方向 owner 或管理员权限', 'Requires research-direction owner or admin permission'), 'error');
      else toast(`${tr('生成链接失败：', 'Create link failed: ')}${e instanceof Error ? e.message : String(e)}`, 'error');
    },
  });

  const busy = addMutation.isPending || removeMutation.isPending;

  async function copyShare() {
    if (!shareUrl) return;
    try {
      await navigator.clipboard.writeText(shareUrl);
      toast(tr('已复制分享链接', 'Share link copied'), 'ok');
    } catch {
      toast(tr('复制失败，请手动选择链接复制', 'Copy failed — select the link and copy manually'), 'error');
    }
  }

  function CollabRow({ c }: { c: CollaboratorRead }) {
    return (
      <div className="row gap8" style={{ padding: '7px 0' }}>
        <span
          style={{
            width: 26,
            height: 26,
            borderRadius: '50%',
            flexShrink: 0,
            background: c.is_owner ? 'var(--accent)' : 'var(--surface-3)',
            color: c.is_owner ? '#fff' : 'var(--text-2)',
            fontSize: 11,
            fontWeight: 700,
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
          }}
        >
          {initials(c.display_name || c.email)}
        </span>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div className="row gap6" style={{ minWidth: 0 }}>
            <span style={{ fontSize: 12.5, fontWeight: 600, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
              {c.display_name || c.email}
            </span>
            {c.is_owner && (
              <span className="pill sm" style={{ height: 16, fontSize: 9.5, padding: '0 6px', background: 'var(--accent-soft)', color: 'var(--accent-text)' }}>
                {tr('负责人', 'Owner')}
              </span>
            )}
          </div>
          <div className="mono" style={{ fontSize: 10.5, color: 'var(--text-4)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
            {c.email}
          </div>
        </div>
        {!c.is_owner && (
          <button
            className="btn btn-ghost sm"
            style={{ height: 24, fontSize: 10.5, padding: '0 8px' }}
            disabled={busy}
            onClick={() => removeMutation.mutate(c.user_id)}
          >
            <Icon name="trash" size={11} />
            {tr('移除', 'Remove')}
          </button>
        )}
      </div>
    );
  }

  return (
    <Modal
      open={open}
      onClose={onClose}
      width={520}
      title={
        <>
          <Icon name="users" size={16} style={{ color: 'var(--accent)' }} />
          {tr('协作者', 'Collaborators')}
        </>
      }
      sub={tr('管理谁能协同编辑这篇论文', 'Manage who can co-edit this manuscript')}
      footer={
        <button className="btn btn-ghost sm" onClick={onClose}>
          {tr('关闭', 'Close')}
        </button>
      }
    >
      {/* 搜索加人 */}
      <div style={{ position: 'relative', marginBottom: 6 }}>
        <input
          className="input"
          style={{ width: '100%', height: 34, fontSize: 12.5 }}
          placeholder={tr('搜索平台用户（姓名 / 邮箱）加入协同…', 'Search platform users (name / email) to add…')}
          value={term}
          onChange={(e) => setTerm(e.target.value)}
        />
        {debounced.length > 0 && (
          <div
            className="scroll"
            style={{
              position: 'absolute',
              top: 38,
              left: 0,
              right: 0,
              zIndex: 5,
              maxHeight: 220,
              overflowY: 'auto',
              background: 'var(--surface)',
              border: '0.5px solid var(--border)',
              borderRadius: 8,
              boxShadow: 'var(--shadow-2, 0 4px 16px rgba(0,0,0,0.12))',
            }}
          >
            {searchQuery.isLoading ? (
              <div className="muted" style={{ fontSize: 11.5, padding: '10px 12px' }}>{tr('搜索中…', 'Searching…')}</div>
            ) : results.length === 0 ? (
              <div className="muted" style={{ fontSize: 11.5, padding: '10px 12px' }}>{tr('没有匹配的用户', 'No matching users')}</div>
            ) : (
              results.map((u) => (
                <div
                  key={u.id}
                  className="writer-file"
                  onClick={() => !addMutation.isPending && addMutation.mutate(u.id)}
                  style={{ padding: '7px 12px', cursor: 'pointer', display: 'flex', alignItems: 'center', gap: 8 }}
                >
                  <span style={{ flex: 1, minWidth: 0 }}>
                    <div style={{ fontSize: 12.5, fontWeight: 600, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                      {u.display_name || u.email}
                    </div>
                    <div className="mono" style={{ fontSize: 10.5, color: 'var(--text-4)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                      {u.email}
                    </div>
                  </span>
                  <Icon name="plus" size={13} style={{ color: 'var(--accent)', flexShrink: 0 }} />
                </div>
              ))
            )}
          </div>
        )}
      </div>

      {/* 协作者列表 */}
      {listQuery.isLoading ? (
        <div className="empty" style={{ padding: 20 }}>{tr('加载协作者…', 'Loading collaborators…')}</div>
      ) : collaborators.length === 0 ? (
        <div className="empty" style={{ padding: 20 }}>{tr('还没有协作者', 'No collaborators yet')}</div>
      ) : (
        <div style={{ borderTop: '0.5px solid var(--border)', marginTop: 4 }}>
          {collaborators.map((c) => (
            <div key={c.user_id} style={{ borderBottom: '0.5px solid var(--border)' }}>
              <CollabRow c={c} />
            </div>
          ))}
        </div>
      )}

      {/* 分享链接 */}
      <div style={{ marginTop: 16, paddingTop: 14, borderTop: '0.5px solid var(--border)' }}>
        <div style={{ fontSize: 11.5, color: 'var(--text-3)', marginBottom: 8 }}>
          {tr('平台用户打开此链接登录后即可协同编辑。', 'Any platform user who opens this link and signs in can co-edit.')}
        </div>
        {shareUrl ? (
          <div className="row gap6">
            <input
              className="input mono"
              readOnly
              value={shareUrl}
              onFocus={(e) => e.currentTarget.select()}
              style={{ flex: 1, minWidth: 0, height: 32, fontSize: 11 }}
            />
            <button className="btn btn-primary sm" onClick={() => void copyShare()}>
              <Icon name="link" size={13} />
              {tr('复制', 'Copy')}
            </button>
          </div>
        ) : (
          <button
            className="btn btn-soft sm"
            disabled={shareMutation.isPending}
            onClick={() => shareMutation.mutate()}
          >
            <Icon name="link" size={13} />
            {shareMutation.isPending ? tr('生成中…', 'Creating…') : tr('生成协同编辑分享链接', 'Create co-editing share link')}
          </button>
        )}
      </div>
    </Modal>
  );
}
