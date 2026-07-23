import { Icon } from '../../components/ui/Icon';
import type { DirectionLibrarySummary } from '../../lib/api';
import { tr } from '../../lib/i18n';

/* ============================================================
   文献库多选清单：勾选卡片。库列表与选中态由外部传入，
   建课题向导与课题设置的「关联文献库」共用。
   ============================================================ */

export function LibraryPicker({
  libraries,
  selectedIds,
  onToggle,
  disabled,
}: {
  libraries: DirectionLibrarySummary[];
  selectedIds: Set<string>;
  onToggle: (id: string) => void;
  disabled?: boolean;
}) {
  return (
    <div className="col gap8">
      {libraries.map((lib) => {
        const on = selectedIds.has(lib.id);
        return (
          <button
            key={lib.id}
            type="button"
            disabled={disabled}
            className="card hoverable"
            onClick={() => onToggle(lib.id)}
            style={{
              padding: '12px 14px',
              display: 'flex',
              gap: 12,
              alignItems: 'center',
              textAlign: 'left',
              cursor: disabled ? 'default' : 'pointer',
              border: on ? '1px solid var(--accent)' : '0.5px solid var(--border)',
              background: on ? 'var(--accent-soft)' : 'var(--surface)',
            }}
          >
            <span
              style={{
                width: 20,
                height: 20,
                borderRadius: 6,
                flexShrink: 0,
                border: on ? 'none' : '1.5px solid var(--border)',
                background: on ? 'var(--accent)' : 'transparent',
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                color: '#fff',
              }}
            >
              {on && <Icon name="check" size={13} />}
            </span>
            <div style={{ flex: 1, minWidth: 0 }}>
              <div className="row gap8">
                <span style={{ fontSize: 13.5, fontWeight: 650 }}>{lib.name}</span>
                {lib.is_mine && (
                  <span className="pill sm" style={{ background: 'var(--accent-soft)', color: 'var(--accent-text)' }}>
                    {tr('我在用', 'In use')}
                  </span>
                )}
              </div>
              <div
                style={{
                  fontSize: 12,
                  color: 'var(--text-3)',
                  marginTop: 3,
                  lineHeight: 1.5,
                  display: '-webkit-box',
                  WebkitLineClamp: 2,
                  WebkitBoxOrient: 'vertical',
                  overflow: 'hidden',
                }}
              >
                {lib.statement ?? tr('这个方向还没有写一句话介绍。', 'No statement yet.')}
              </div>
              <div className="row gap10" style={{ fontSize: 11, color: 'var(--text-4)', marginTop: 4 }}>
                <span>{tr(`${lib.paper_count} 篇论文`, `${lib.paper_count} papers`)}</span>
                <span>{tr(`${lib.concept_count} 个概念`, `${lib.concept_count} concepts`)}</span>
              </div>
            </div>
          </button>
        );
      })}
    </div>
  );
}
