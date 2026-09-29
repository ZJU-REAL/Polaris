/* ============================================================
   设置页导航：左侧分组竖排（主区窄时换成分组下拉框）。

   历程：
   - 最早是一整条 Segmented：十七八个标签挤一行，中文被压成一字一行；
   - 然后改成两行可折行的标签（个人 / 工作区）：不再断字，但两行的起点随组名宽度错开，
     组名和标签长得差不多、分不出层级，窗口一变宽窄整排就重新折一遍。

   设置项多到十几个时，常规做法是左侧竖排导航：组名是小号灰字的分节标题，下面一项
   一行，永远对齐、加项不会挤。选中态沿用主侧栏的 .nav-item 口径（浅主题底 + 主题色字），
   两处导航看起来是一套东西。主区窄于 900px（Buddy 拉开或小窗）时竖栏会吃掉太多正文
   宽度，换成一个按组分段的下拉框。
   ============================================================ */
import type { ReactNode } from 'react';

export interface SettingsTabGroup<V extends string> {
  label: string;
  items: { v: V; label: string }[];
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
  const visible = groups.filter((g) => g.items.length > 0);
  return (
    <>
      <nav className="settings-nav" aria-label="Settings">
        {visible.map((group) => (
          <div key={group.label} className="settings-nav-group">
            <div className="settings-nav-heading">{group.label}</div>
            {group.items.map((item) => (
              <button
                key={item.v}
                type="button"
                className={'settings-nav-item' + (item.v === value ? ' active' : '')}
                aria-current={item.v === value ? 'page' : undefined}
                onClick={() => onChange(item.v)}
              >
                {item.label}
              </button>
            ))}
          </div>
        ))}
      </nav>
      <select
        className="input settings-nav-select"
        value={value}
        aria-label="Settings section"
        onChange={(e) => onChange(e.target.value as V)}
      >
        {visible.map((group) => (
          <optgroup key={group.label} label={group.label}>
            {group.items.map((item) => (
              <option key={item.v} value={item.v}>
                {item.label}
              </option>
            ))}
          </optgroup>
        ))}
      </select>
    </>
  );
}

/** 设置页的两栏骨架：左导航、右内容。 */
export function SettingsLayout({ nav, children }: { nav: ReactNode; children: ReactNode }) {
  return (
    <div className="settings-layout">
      <aside className="settings-layout-nav">{nav}</aside>
      <div className="settings-layout-main">{children}</div>
    </div>
  );
}
