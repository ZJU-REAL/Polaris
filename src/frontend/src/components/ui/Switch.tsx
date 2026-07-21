export interface SwitchProps {
  checked: boolean;
  onChange: (checked: boolean) => void;
  disabled?: boolean;
  /** 无可见标签时提供；有标签时用 aria-labelledby 关联。 */
  'aria-label'?: string;
  'aria-labelledby'?: string;
  id?: string;
}

/** 小巧的开关（受控）。button 原生支持 Enter/Space，样式见 global.css `.switch`。 */
export function Switch({ checked, onChange, disabled, id, ...aria }: SwitchProps) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      id={id}
      className={`switch${checked ? ' on' : ''}`}
      disabled={disabled}
      onClick={() => onChange(!checked)}
      aria-label={aria['aria-label']}
      aria-labelledby={aria['aria-labelledby']}
    >
      <span className="switch-thumb" />
    </button>
  );
}
