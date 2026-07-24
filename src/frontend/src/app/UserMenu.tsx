import { useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { Avatar } from '../components/ui/Avatar';
import { Icon } from '../components/ui/Icon';
import { Modal } from '../components/ui/Modal';
import { useAuth } from './auth';
import { isAdmin, type UserRead } from '../lib/api';
import { tr } from '../lib/i18n';

/* 侧栏底部用户区：点头像弹出菜单（关于我们 / 设置 / 退出登录）。邀请协作者入口在研究方向详情页。 */

/* —— 关于我们弹窗：实验室介绍 + 主页/项目链接 + 小红书二维码 —— */
function AboutModal({ open, onClose }: { open: boolean; onClose: () => void }) {
  return (
    <Modal open={open} onClose={onClose} title={tr('关于我们', 'About us')} width={440}>
      <div className="col gap16" style={{ padding: '2px 2px 4px' }}>
        <div style={{ fontSize: 13, lineHeight: 1.75, color: 'var(--text-2)' }}>
          {tr(
            'Polaris 由浙江大学 REAL Lab 研发——一个面向科研全流程的 AI 科研智能体平台。',
            'Polaris is built by the REAL Lab at Zhejiang University — an AI research-agent platform for the whole research workflow.',
          )}
        </div>
        <div className="col gap8">
          <a
            className="row gap8"
            href="https://zju-real.github.io/"
            target="_blank"
            rel="noreferrer noopener"
            style={{ color: 'var(--accent-text)', fontSize: 12.5, textDecoration: 'none' }}
          >
            <Icon name="link" size={13} />
            {tr('实验室主页', 'Lab homepage')}
            <span className="mono" style={{ color: 'var(--text-4)', fontSize: 11 }}>zju-real.github.io</span>
          </a>
          <a
            className="row gap8"
            href="https://github.com/ZJU-REAL/Polaris"
            target="_blank"
            rel="noreferrer noopener"
            style={{ color: 'var(--accent-text)', fontSize: 12.5, textDecoration: 'none' }}
          >
            <Icon name="link" size={13} />
            {tr('项目地址（GitHub）', 'Project (GitHub)')}
            <span className="mono" style={{ color: 'var(--text-4)', fontSize: 11 }}>ZJU-REAL/Polaris</span>
          </a>
        </div>
        <div
          className="col gap8"
          style={{ alignItems: 'center', paddingTop: 6, borderTop: '0.5px solid var(--border)' }}
        >
          <div
            style={{
              width: 168,
              height: 224,
              borderRadius: 10,
              background: 'var(--surface-2)',
              border: '0.5px solid var(--border)',
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              overflow: 'hidden',
            }}
          >
            <img
              src="/xhs-qr.jpg"
              alt={tr('小红书二维码', 'RED (Xiaohongshu) QR')}
              style={{ width: '100%', height: '100%', objectFit: 'contain' }}
              onError={(e) => {
                e.currentTarget.style.display = 'none';
              }}
            />
          </div>
          <span style={{ fontSize: 11.5, color: 'var(--text-4)' }}>
            {tr('扫码关注我们的小红书', 'Scan to follow us on RED')}
          </span>
        </div>
      </div>
    </Modal>
  );
}

export function UserMenu({ me, collapsed }: { me: UserRead | undefined; collapsed: boolean }) {
  const navigate = useNavigate();
  const { logout } = useAuth();
  const [open, setOpen] = useState(false);
  const [aboutOpen, setAboutOpen] = useState(false);
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
              setAboutOpen(true);
            }}
          >
            <Icon name="sparkle" size={15} />
            {tr('关于我们', 'About us')}
          </button>
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
      <AboutModal open={aboutOpen} onClose={() => setAboutOpen(false)} />
    </div>
  );
}
