import { useRef, useState, type ReactNode } from 'react';
import { Icon, type IconName } from './Icon';
import { useClickOutside } from './SelectMenu';
import { tr } from '../../lib/i18n';
import './DotsMenu.css';

/* ============================================================
   「更多」菜单：一个安静的三点按钮（或带文字的按钮）+ 弹出的一列动作。
   一页只留一个主按钮，其余不常用的动作收在这里。
   ============================================================ */

export interface DotsMenuItem {
  key: string;
  label: string;
  icon?: IconName;
  /** 危险动作（删除类）用红字 */
  danger?: boolean;
  disabled?: boolean;
  /** 悬停说明（禁用时写原因） */
  title?: string;
  /** 这一项上面加一条分隔线 */
  separated?: boolean;
  onSelect: () => void;
}

export interface DotsMenuProps {
  items: DotsMenuItem[];
  /** 按钮的读屏名，默认「更多」 */
  label?: string;
  /** 给了就显示成带文字的按钮，否则是三点图标 */
  text?: ReactNode;
  /** 菜单靠哪边展开，默认右对齐 */
  align?: 'left' | 'right';
  /** 三点按钮不要边框（卡片里用） */
  plain?: boolean;
}

export function DotsMenu({ items, label, text, align = 'right', plain }: DotsMenuProps) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  useClickOutside(ref, open, () => setOpen(false));
  if (items.length === 0) return null;
  const name = label ?? tr('更多', 'More');
  return (
    <div
      className="dots-menu"
      ref={ref}
      onKeyDown={(e) => {
        if (e.key === 'Escape' && open) {
          e.stopPropagation();
          setOpen(false);
        }
      }}
    >
      {text ? (
        <button type="button" className="btn btn-plain sm" aria-haspopup="menu" aria-expanded={open} onClick={() => setOpen((o) => !o)}>
          {text}
          <Icon name="chevDown" size={11} />
        </button>
      ) : (
        <button
          type="button"
          className={'dots-menu-btn' + (plain ? ' is-plain' : '')}
          aria-haspopup="menu"
          aria-expanded={open}
          aria-label={name}
          title={name}
          onClick={() => setOpen((o) => !o)}
        >
          <span className="dots-menu-dots" aria-hidden="true">
            <i />
            <i />
            <i />
          </span>
        </button>
      )}
      {open && (
        <div className={'dots-menu-list' + (align === 'left' ? ' is-left' : '')} role="menu">
          {items.map((it) => (
            <div key={it.key} style={{ display: 'contents' }}>
              {it.separated && <div className="user-menu-sep" />}
              <button
                type="button"
                role="menuitem"
                className={'user-menu-item' + (it.danger ? ' danger' : '')}
                disabled={it.disabled}
                title={it.title}
                onClick={() => {
                  setOpen(false);
                  it.onSelect();
                }}
              >
                {it.icon && <Icon name={it.icon} size={14} />}
                {it.label}
              </button>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
