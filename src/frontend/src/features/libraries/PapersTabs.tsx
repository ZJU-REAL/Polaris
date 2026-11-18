import { Link } from 'react-router-dom';
import { tr } from '../../lib/i18n';
import '../../app/DirectionNav.css';

/* 「论文」下的两页共用的页签：文献库（按方向收集的论文）和我的文献库（自己收藏、读过的）。
   侧栏只有一个「论文」入口，这两页靠这排页签互相到达——样式与课题页头的页签一致。 */
export function PapersTabs({ active }: { active: 'libraries' | 'mine' }) {
  const tabs = [
    { key: 'libraries' as const, to: '/libraries', label: tr('文献库', 'Libraries') },
    { key: 'mine' as const, to: '/library', label: tr('我的文献库', 'My library') },
  ];
  return (
    <header className="dn" style={{ flexShrink: 0 }}>
      <nav className="dn-tabs" style={{ marginTop: 0 }} aria-label={tr('论文', 'Papers')}>
        {tabs.map((t) => (
          <Link
            key={t.key}
            to={t.to}
            className={'dn-tab' + (t.key === active ? ' is-active' : '')}
            aria-current={t.key === active ? 'page' : undefined}
          >
            {t.label}
          </Link>
        ))}
      </nav>
    </header>
  );
}
