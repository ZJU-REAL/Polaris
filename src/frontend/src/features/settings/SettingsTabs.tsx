/* ============================================================
   设置页的标签导航：按组分行、可折行。

   以前是一整条 Segmented：十七八个标签挤在一行里，不折行也不滚动，每个按钮被压到
   只剩一个字宽——中文标签竖着一字一行（「个/人/信/息」），英文则挤成两三行。
   Segmented 是给两三个选项的切换用的，撑不住整页导航。

   这里改成两组（个人 / 工作区），每组一行标签、放不下就折到下一行，标签本身永不断行。
   ============================================================ */
import type { ReactNode } from 'react';

export interface SettingsTabGroup<V extends string> {
  label: ReactNode;
  items: { v: V; label: ReactNode }[];
}

export function SettingsTabs<V extends string>({
  groups,
  value,
  onChange,
}: {
  groups: SettingsTabGroup<V>[];
  value: V;
  onChange: (v: V) => void;
}) {
  return (
    <nav className="settings-tabs" style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
      {groups
        .filter((g) => g.items.length > 0)
        .map((group, i) => (
          <div key={i} style={{ display: 'flex', alignItems: 'baseline', gap: 10 }}>
            <span
              style={{
                flexShrink: 0,
                width: 56,
                fontSize: 11.5,
                fontWeight: 650,
                color: 'var(--text-3)',
                whiteSpace: 'nowrap',
              }}
            >
              {group.label}
            </span>
            <div role="tablist" style={{ display: 'flex', flexWrap: 'wrap', gap: 4, minWidth: 0 }}>
              {group.items.map((item) => {
                const on = item.v === value;
                return (
                  <button
                    key={item.v}
                    type="button"
                    role="tab"
                    aria-selected={on}
                    onClick={() => onChange(item.v)}
                    style={{
                      border: on ? '0.5px solid var(--border-2)' : '0.5px solid transparent',
                      cursor: 'pointer',
                      borderRadius: 7,
                      padding: '5px 12px',
                      fontSize: 12.5,
                      fontWeight: 600,
                      fontFamily: 'var(--sans)',
                      whiteSpace: 'nowrap',
                      background: on ? 'var(--surface)' : 'transparent',
                      color: on ? 'var(--text)' : 'var(--text-3)',
                      boxShadow: on ? 'var(--shadow-card)' : 'none',
                      transition: 'all .12s',
                    }}
                  >
                    {item.label}
                  </button>
                );
              })}
            </div>
          </div>
        ))}
    </nav>
  );
}
