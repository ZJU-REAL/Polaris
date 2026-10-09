import { useRef, useState, type CSSProperties } from 'react';
import { Icon, type IconName } from '../../components/ui/Icon';
import { useClickOutside } from '../../components/ui/SelectMenu';
import { tr } from '../../lib/i18n';

/* ============================================================
   编辑器顶栏的「更多」菜单：把次要操作（AI 使用声明、更新参考文献、
   导出 arXiv、投稿）收进来，顶栏只留编译这一个主操作，窄屏不再溢出。
   ============================================================ */

export interface MoreMenuItem {
  key: string;
  label: string;
  icon: IconName;
  /** 悬停说明（禁用时说明原因） */
  title?: string;
  disabled?: boolean;
  onSelect: () => void;
}

const itemStyle: CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  gap: 8,
  width: '100%',
  padding: '8px 12px',
  border: 'none',
  background: 'transparent',
  cursor: 'pointer',
  fontSize: 12,
  fontFamily: 'var(--sans)',
  color: 'var(--text)',
  textAlign: 'left',
  whiteSpace: 'nowrap',
};

export function MoreMenu({ items }: { items: MoreMenuItem[] }) {
  const [open, setOpen] = useState(false);
  const wrapRef = useRef<HTMLDivElement>(null);
  useClickOutside(wrapRef, open, () => setOpen(false));

  return (
    <div ref={wrapRef} style={{ position: 'relative', flexShrink: 0 }}>
      <button
        type="button"
        className="btn btn-ghost sm"
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => setOpen((o) => !o)}
      >
        {tr('更多', 'More')}
        <Icon name="chevDown" size={12} />
      </button>
      {open && (
        <div
          className="card"
          role="menu"
          style={{
            position: 'absolute',
            top: 'calc(100% + 4px)',
            right: 0,
            zIndex: 30,
            minWidth: 200,
            padding: '4px 0',
            boxShadow: 'var(--shadow-pop)',
          }}
        >
          {items.map((it) => (
            <button
              key={it.key}
              type="button"
              role="menuitem"
              title={it.title}
              disabled={it.disabled}
              style={{ ...itemStyle, ...(it.disabled ? { opacity: 0.45, cursor: 'not-allowed' } : null) }}
              onClick={() => {
                setOpen(false);
                it.onSelect();
              }}
            >
              <Icon name={it.icon} size={13} style={{ color: 'var(--text-3)' }} />
              {it.label}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
