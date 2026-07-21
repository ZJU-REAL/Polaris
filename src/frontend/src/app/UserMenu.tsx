import { useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { Avatar } from '../components/ui/Avatar';
import { Icon } from '../components/ui/Icon';
import { useAuth } from './auth';
import { isAdmin, type UserRead } from '../lib/api';
import { tr } from '../lib/i18n';

/* 侧栏底部用户区：点头像弹出菜单（设置 / 退出登录）。邀请协作者入口在研究方向详情页。 */

export function UserMenu({ me, collapsed }: { me: UserRead | undefined; collapsed: boolean }) {
  const navigate = useNavigate();
  const { logout } = useAuth();
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);

  // 点击菜单外部 / Esc 关闭（菜单是 rootRef 的子元素，contains 即可覆盖）
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
        <Avatar userId={me?.id} hasAvatar={!!me?.has_avatar} name={name} size={34} />
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
    </div>
  );
}
