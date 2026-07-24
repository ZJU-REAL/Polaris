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
    <Modal open={open} onClose={onClose} title={tr('关于我们', 'About us')} width={680}>
      <div className="row gap20" style={{ alignItems: 'flex-start', flexWrap: 'wrap', padding: '2px 2px 4px' }}>
        {/* 左栏：小红书二维码 */}
        <div className="col gap8" style={{ alignItems: 'center', flexShrink: 0 }}>
          <div
            style={{
              width: 312,
              height: 416,
              borderRadius: 12,
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

        {/* 右栏：介绍 */}
        <div className="col gap14" style={{ flex: 1, minWidth: 280 }}>
          <div className="col gap6">
            <div style={{ fontSize: 15, fontWeight: 700, letterSpacing: 0.2, color: 'var(--text-1)' }}>
              {tr('浙江大学 REAL Lab', 'REAL Lab · Zhejiang University')}
            </div>
            <div style={{ fontSize: 12.5, fontWeight: 620, color: 'var(--accent-text)' }}>
              {tr(
                '会推理、有具身、能自主、可成长，面向真实世界的通用智能',
                'Reasoning · Embodied · Agentic · Lifelong-learning AI',
              )}
            </div>
          </div>
          <div style={{ fontSize: 13, lineHeight: 1.75, color: 'var(--text-2)' }}>
            {tr(
              'REAL 聚焦通向通用智能的四大核心能力：推理（Reasoning）赋予模型深度思考与复杂问题求解的能力，是智能的认知基座；具身（Embodied）让智能走出屏幕，通过物理本体感知并作用于真实环境；智能体（Agentic）使系统能够自主规划、调用工具、执行长程任务；终身学习（Lifelong）则让智能体在与环境的持续交互中不断积累经验、自我进化。四者层层递进，会思考、有身体、能行动、可成长，共同指向一个目标：构建真正走进真实世界（REAL world）的通用智能。',
              'REAL: Reasoning, Embodied, Agentic, and Lifelong-learning AI. Toward AI that thinks, acts, and grows in the real world.',
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
