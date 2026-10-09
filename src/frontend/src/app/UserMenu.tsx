import { useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { Avatar } from '../components/ui/Avatar';
import { Icon } from '../components/ui/Icon';
import { Modal } from '../components/ui/Modal';
import type { UserRead } from '../lib/api';
import { tr } from '../lib/i18n';

/* 侧栏底部用户区：点头像弹出菜单（关于 / 设置）。单人本地产品没有「退出登录」。 */

/* —— 关于弹窗：产品简介 + 开源仓库链接。只讲产品，不带机构品牌 —— */
function AboutModal({ open, onClose }: { open: boolean; onClose: () => void }) {
  return (
    <Modal open={open} onClose={onClose} title={tr('关于', 'About')} width={480}>
      <div className="col gap14" style={{ padding: '2px 2px 4px' }}>
        <div className="col gap6">
          <div style={{ fontSize: 15, fontWeight: 600, letterSpacing: 0.2, color: 'var(--text-1)' }}>
            Polaris
          </div>
          <div style={{ fontSize: 13, fontWeight: 600, color: 'var(--accent-text)' }}>
            {tr('个人科研工作台', 'A research workbench for individual researchers')}
          </div>
        </div>
        <div style={{ fontSize: 13, lineHeight: 1.75, color: 'var(--text-2)' }}>
          {tr(
            '从读文献、想点子、做实验到写论文，都在一个地方完成。',
            'Read papers, develop ideas, run experiments and write papers, all in one place.',
          )}
        </div>
        {/* 仓库链接指向真实 remote（项目事实信息），不算机构品牌文案 */}
        <a
          className="row gap8"
          href="https://github.com/ZJU-REAL/Polaris"
          target="_blank"
          rel="noreferrer noopener"
          style={{ color: 'var(--accent-text)', fontSize: 13, textDecoration: 'none' }}
        >
          <Icon name="link" size={13} />
          {tr('GitHub 源码', 'Source code on GitHub')}
        </a>
      </div>
    </Modal>
  );
}

export function UserMenu({ me, collapsed }: { me: UserRead | undefined; collapsed: boolean }) {
  const navigate = useNavigate();
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
            {tr('关于', 'About')}
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
        </div>
      )}
      <AboutModal open={aboutOpen} onClose={() => setAboutOpen(false)} />
    </div>
  );
}
