import { useRef, useState, type CSSProperties } from 'react';
import { Icon, type IconName } from './Icon';
import { useClickOutside } from './SelectMenu';
import { tr } from '../../lib/i18n';

/* ============================================================
   「导出」按钮 + 弹出菜单（文献工作台的整库导出、各列表的多选导出共用）。
   菜单项由调用方给：某个作用域后端不支持的格式就不要传进来。
   ============================================================ */

export type ExportDropdownItem = {
  key: string;
  /** 主文案（调用方 tr 好） */
  label: string;
  /** 右侧灰字（扩展名 / 适用范围） */
  hint?: string;
  icon?: IconName;
  onSelect: () => void;
};

/** 多选导出的菜单项：引用导出端点（课题 / 库 / 个人库 / 每日）都支持这两种格式；
    Obsidian 是整库 zip 端点、不吃选中的 id，所以不放进多选菜单。 */
export function citationExportItems(
  onPick: (format: 'bibtex' | 'csl-json') => void,
): ExportDropdownItem[] {
  return [
    { key: 'bibtex', icon: 'book', label: 'BibTeX', hint: '.bib', onSelect: () => onPick('bibtex') },
    { key: 'csl-json', icon: 'layers', label: 'CSL-JSON', hint: '.json', onSelect: () => onPick('csl-json') },
  ];
}

export function ExportDropdown({
  items,
  busy = false,
  disabled = false,
  sm = false,
  /** 菜单弹出方向：底部操作栏用 'up'，页头按钮用 'down' */
  placement = 'down',
  /** 菜单对齐：跟随触发按钮所在的一侧 */
  align = 'right',
  minWidth = 200,
}: {
  items: ExportDropdownItem[];
  busy?: boolean;
  disabled?: boolean;
  sm?: boolean;
  placement?: 'up' | 'down';
  align?: 'left' | 'right';
  minWidth?: number;
}) {
  const [open, setOpen] = useState(false);
  const wrapRef = useRef<HTMLDivElement>(null);
  useClickOutside(wrapRef, open, () => setOpen(false));

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
  };

  return (
    <div ref={wrapRef} style={{ position: 'relative' }}>
      <button
        type="button"
        className={'btn btn-ghost' + (sm ? ' sm' : '')}
        disabled={disabled || busy}
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => setOpen((o) => !o)}
      >
        {busy ? (
          <Icon name="refresh" size={sm ? 12 : 14} style={{ animation: 'spin 1s linear infinite' }} />
        ) : (
          <Icon name="download" size={sm ? 12 : 14} />
        )}
        {tr('导出', 'Export')}
        <Icon name="chevDown" size={12} />
      </button>
      {open && (
        <div
          className="card"
          role="menu"
          style={{
            position: 'absolute',
            ...(placement === 'up' ? { bottom: 'calc(100% + 4px)' } : { top: 'calc(100% + 4px)' }),
            ...(align === 'right' ? { right: 0 } : { left: 0 }),
            zIndex: 30,
            minWidth,
            padding: '4px 0',
            boxShadow: 'var(--shadow-pop)',
          }}
        >
          {items.map((it) => (
            <button
              key={it.key}
              type="button"
              role="menuitem"
              style={itemStyle}
              onClick={() => {
                setOpen(false);
                it.onSelect();
              }}
            >
              {it.icon && <Icon name={it.icon} size={13} style={{ color: 'var(--text-3)' }} />}
              <span>
                {it.label}
                {it.hint && (
                  <span className="muted" style={{ marginLeft: 5, fontSize: 10.5 }}>
                    {it.hint}
                  </span>
                )}
              </span>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
