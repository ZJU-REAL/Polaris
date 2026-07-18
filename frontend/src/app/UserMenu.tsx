import { useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Avatar } from '../components/ui/Avatar';
import { Icon } from '../components/ui/Icon';
import { Modal } from '../components/ui/Modal';
import { toast } from '../components/ui/Toast';
import { useAuth } from './auth';
import { useProject } from './project';
import { api, isAdmin, type UserRead } from '../lib/api';
import { fmtTime } from '../lib/format';
import { tr } from '../lib/i18n';

/* 侧栏底部用户区：点头像弹出菜单（设置 / 邀请协作者 / 退出登录）。 */

export function UserMenu({ me, collapsed }: { me: UserRead | undefined; collapsed: boolean }) {
  const navigate = useNavigate();
  const { logout } = useAuth();
  const [open, setOpen] = useState(false);
  const [inviteOpen, setInviteOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);

  // 点击菜单外部 / Esc 关闭
  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpen(false);
    };
    document.addEventListener('mousedown', onDown);
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('mousedown', onDown);
      document.removeEventListener('keydown', onKey);
    };
  }, [open]);

  const name = me?.display_name || me?.email || tr('研究员', 'Researcher');

  return (
    <div className="user-menu-root" ref={rootRef}>
      <button
        className={'user-trigger' + (open ? ' open' : '')}
        onClick={() => setOpen((o) => !o)}
        title={collapsed ? name : undefined}
        aria-haspopup="menu"
        aria-expanded={open}
      >
        <Avatar userId={me?.id} hasAvatar={!!me?.has_avatar} name={name} size={26} />
        <span className="user-info">
          <span className="user-name">{name}</span>
          <span className="user-role">{isAdmin(me) ? tr('管理员', 'Admin') : tr('研究员', 'Researcher')}</span>
        </span>
        {!collapsed && (
          <Icon
            name="chevDown"
            size={13}
            style={{ color: 'var(--text-3)', flexShrink: 0, transform: open ? 'none' : 'rotate(180deg)', transition: 'transform 0.15s' }}
          />
        )}
      </button>

      {open && (
        <div className="user-menu" role="menu">
          <button
            className="user-menu-item"
            role="menuitem"
            onClick={() => {
              setOpen(false);
              navigate('/settings');
            }}
          >
            <Icon name="settings" size={15} />
            {tr('设置', 'Settings')}
          </button>
          <button
            className="user-menu-item"
            role="menuitem"
            onClick={() => {
              setOpen(false);
              setInviteOpen(true);
            }}
          >
            <Icon name="users" size={15} />
            {tr('邀请协作者', 'Invite collaborator')}
          </button>
          <div className="user-menu-sep" />
          <button
            className="user-menu-item danger"
            role="menuitem"
            onClick={() => {
              setOpen(false);
              logout();
              navigate('/login');
            }}
          >
            <Icon name="logout" size={15} />
            {tr('退出登录', 'Log out')}
          </button>
        </div>
      )}

      {inviteOpen && <InviteDialog onClose={() => setInviteOpen(false)} />}
    </div>
  );
}

/** 邀请协作者：为当前研究方向生成/管理邀请链接。 */
function InviteDialog({ onClose }: { onClose: () => void }) {
  const queryClient = useQueryClient();
  const { currentProjectId, currentProject } = useProject();

  const { data: invites } = useQuery({
    queryKey: ['invites', currentProjectId],
    queryFn: () => api.listInvites(currentProjectId!),
    enabled: !!currentProjectId,
    retry: false,
  });

  const createMutation = useMutation({
    mutationFn: () => api.createInvite(currentProjectId!, { expires_days: 7 }),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ['invites', currentProjectId] }),
    onError: (e) => toast(`${tr('生成失败', 'Failed to create')}：${e instanceof Error ? e.message : String(e)}`, 'error'),
  });
  const revokeMutation = useMutation({
    mutationFn: (id: string) => api.revokeInvite(currentProjectId!, id),
    onSuccess: () => {
      toast(tr('邀请链接已撤销', 'Invite link revoked'), 'ok');
      void queryClient.invalidateQueries({ queryKey: ['invites', currentProjectId] });
    },
    onError: (e) => toast(`${tr('撤销失败', 'Failed to revoke')}：${e instanceof Error ? e.message : String(e)}`, 'error'),
  });

  const copy = (token: string) => {
    void navigator.clipboard.writeText(`${window.location.origin}/join/${token}`).then(
      () => toast(tr('邀请链接已复制', 'Invite link copied'), 'ok'),
      () => toast(tr('复制失败，请手动复制', 'Copy failed, copy it manually'), 'error'),
    );
  };

  return (
    <Modal
      open
      onClose={onClose}
      width={520}
      title={tr('邀请协作者', 'Invite collaborator')}
      sub={
        currentProject
          ? tr(`加入研究方向：${currentProject.name}`, `Join direction: ${currentProject.name}`)
          : tr('已注册用户打开链接即可加入当前研究方向', 'Registered users open the link to join')
      }
    >
      {!currentProjectId ? (
        <div className="empty" style={{ padding: 24 }}>
          {tr('请先在顶部选择一个研究方向，再生成邀请链接。', 'Select a research direction first, then create an invite link.')}
        </div>
      ) : (
        <>
          <div className="row" style={{ marginBottom: 12 }}>
            <span style={{ fontSize: 12.5, color: 'var(--text-2)' }}>
              {tr('生成一条链接，发给同学即可加入本方向协作。', 'Create a link and share it to invite collaborators.')}
            </span>
            <button
              className="btn btn-primary sm"
              style={{ marginLeft: 'auto' }}
              disabled={createMutation.isPending}
              onClick={() => createMutation.mutate()}
            >
              <Icon name="plus" size={13} />
              {tr('生成链接（7 天有效）', 'Create link (7 days)')}
            </button>
          </div>
          {(invites ?? []).length === 0 ? (
            <div className="empty" style={{ padding: 20 }}>{tr('还没有有效的邀请链接', 'No active invite links yet')}</div>
          ) : (
            <div className="col gap6">
              {invites!.map((inv) => (
                <div key={inv.id} className="row gap8" style={{ padding: '8px 10px', background: 'var(--surface-2)', borderRadius: 9 }}>
                  <span className="mono" style={{ fontSize: 11, color: 'var(--text-2)', flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                    {`${window.location.origin}/join/${inv.token}`}
                  </span>
                  <span style={{ fontSize: 10.5, color: 'var(--text-3)', flexShrink: 0 }}>
                    {tr(`已用 ${inv.used_count} 次`, `${inv.used_count} used`)}
                    {inv.expires_at ? ` · ${fmtTime(inv.expires_at)}` : ''}
                  </span>
                  <button className="btn btn-ghost sm" onClick={() => copy(inv.token)}>{tr('复制', 'Copy')}</button>
                  <button className="btn btn-ghost sm" onClick={() => revokeMutation.mutate(inv.id)}>{tr('撤销', 'Revoke')}</button>
                </div>
              ))}
            </div>
          )}
        </>
      )}
    </Modal>
  );
}
