import { useState } from 'react';
import { Icon } from '../../components/ui/Icon';
import type { ManuscriptFileMeta } from '../../lib/api';
import { tr } from '../../lib/i18n';

/* ============================================================
   左侧文件树（220px）：文件列表（readonly 锁图标）、当前文件
   高亮、新建/重命名/删除小按钮。样式文件与自动生成文件
   （.sty/.cls/.bst、references.bib、figures/）只读，不给操作按钮。
   ============================================================ */

export interface FileTreeProps {
  files: ManuscriptFileMeta[];
  currentId: string | null;
  busy?: boolean;
  onSelect: (f: ManuscriptFileMeta) => void;
  onCreate: (path: string) => void;
  onRename: (f: ManuscriptFileMeta, path: string) => void;
  onDelete: (f: ManuscriptFileMeta) => void;
}

/** 锁图标（Icon 集里没有，就地画一个）。 */
function LockIcon({ size = 11 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" style={{ display: 'block', flexShrink: 0 }}>
      <rect x="5" y="11" width="14" height="9" rx="2" fill="none" stroke="currentColor" strokeWidth="1.8" />
      <path d="M8 11V7a4 4 0 0 1 8 0v4" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
    </svg>
  );
}

export function FileTree({ files, currentId, busy, onSelect, onCreate, onRename, onDelete }: FileTreeProps) {
  const [adding, setAdding] = useState(false);
  const [newPath, setNewPath] = useState('');

  const sorted = [...files].sort((a, b) => a.path.localeCompare(b.path));

  function submitNew() {
    const p = newPath.trim();
    if (!p) {
      setAdding(false);
      return;
    }
    onCreate(p);
    setNewPath('');
    setAdding(false);
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', minHeight: 0, height: '100%' }}>
      <div
        className="row"
        style={{ padding: '10px 12px 8px', justifyContent: 'space-between', flexShrink: 0 }}
      >
        <span style={{ fontSize: 11, fontWeight: 650, color: 'var(--text-3)', letterSpacing: '0.04em' }}>
          {tr('文件', 'FILES')}
        </span>
        <button
          className="icon-btn"
          style={{ width: 22, height: 22, borderRadius: 6 }}
          title={tr('新建文件', 'New file')}
          disabled={busy}
          onClick={() => setAdding((v) => !v)}
        >
          <Icon name="plus" size={12} />
        </button>
      </div>

      {adding && (
        <div style={{ padding: '0 10px 8px', flexShrink: 0 }}>
          <input
            className="input mono"
            autoFocus
            style={{ height: 28, fontSize: 11.5, width: '100%' }}
            placeholder={tr('如 appendix.tex', 'e.g. appendix.tex')}
            value={newPath}
            onChange={(e) => setNewPath(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') submitNew();
              if (e.key === 'Escape') {
                setAdding(false);
                setNewPath('');
              }
            }}
            onBlur={submitNew}
          />
        </div>
      )}

      <div className="scroll" style={{ flex: 1, overflowY: 'auto', padding: '0 8px 10px' }}>
        {sorted.length === 0 && (
          <div style={{ fontSize: 11.5, color: 'var(--text-4)', padding: '8px 6px' }}>{tr('还没有文件', 'No files yet')}</div>
        )}
        {sorted.map((f) => {
          const active = f.id === currentId;
          return (
            <div
              key={f.id}
              className="row gap6 writer-file"
              onClick={() => onSelect(f)}
              title={f.path}
              style={{
                padding: '5px 8px',
                borderRadius: 7,
                cursor: 'pointer',
                background: active ? 'var(--accent-soft)' : undefined,
                color: active ? 'var(--accent-text)' : 'var(--text-2)',
                fontWeight: active ? 600 : 500,
              }}
            >
              <span style={{ color: active ? 'var(--accent)' : 'var(--text-4)', flexShrink: 0, display: 'flex' }}>
                <Icon name="file" size={12} />
              </span>
              <span
                className="mono"
                style={{
                  flex: 1,
                  minWidth: 0,
                  fontSize: 11.5,
                  overflow: 'hidden',
                  textOverflow: 'ellipsis',
                  whiteSpace: 'nowrap',
                }}
              >
                {f.path}
              </span>
              {f.readonly ? (
                <span title={tr('只读文件（模板样式 / 自动生成）', 'Read-only file (template style / auto-generated)')} style={{ color: 'var(--text-4)', display: 'flex' }}>
                  <LockIcon />
                </span>
              ) : (
                <span className="row gap6 writer-file-ops" onClick={(e) => e.stopPropagation()}>
                  <button
                    className="writer-mini-btn"
                    title={tr('重命名', 'Rename')}
                    disabled={busy}
                    onClick={() => {
                      const next = window.prompt(tr('新文件名（含路径）', 'New file name (with path)'), f.path);
                      if (next && next.trim() && next.trim() !== f.path) onRename(f, next.trim());
                    }}
                  >
                    <Icon name="pen" size={11} />
                  </button>
                  <button
                    className="writer-mini-btn"
                    title={tr('删除', 'Delete')}
                    disabled={busy}
                    onClick={() => {
                      if (window.confirm(tr(`确定删除 ${f.path}？删除后无法恢复。`, `Delete ${f.path}? This cannot be undone.`))) onDelete(f);
                    }}
                  >
                    <Icon name="trash" size={11} />
                  </button>
                </span>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}
