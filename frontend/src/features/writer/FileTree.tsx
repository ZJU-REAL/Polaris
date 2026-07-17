import { useMemo, useState } from 'react';
import { Icon } from '../../components/ui/Icon';
import { ConfirmModal } from '../../components/ui/ConfirmModal';
import { PromptModal } from '../../components/ui/PromptModal';
import type { ManuscriptFileMeta } from '../../lib/api';

/* ============================================================
   左侧文件树（220px）：按目录分组（figures/ 等可折叠），
   readonly 锁图标、当前文件高亮、新建/重命名/删除。
   重命名/删除用体系内对话框（不再用 window.prompt/confirm）。
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

function FolderIcon({ size = 12 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" style={{ display: 'block', flexShrink: 0 }}>
      <path
        d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7z"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.8"
        strokeLinejoin="round"
      />
    </svg>
  );
}

/** 按顶层目录分组：目录组在前（字典序），根文件在后（字典序）。 */
function groupFiles(files: ManuscriptFileMeta[]) {
  const roots: ManuscriptFileMeta[] = [];
  const dirs = new Map<string, ManuscriptFileMeta[]>();
  for (const f of files) {
    const slash = f.path.indexOf('/');
    if (slash === -1) {
      roots.push(f);
    } else {
      const dir = f.path.slice(0, slash);
      const list = dirs.get(dir) ?? [];
      list.push(f);
      dirs.set(dir, list);
    }
  }
  const byPath = (a: ManuscriptFileMeta, b: ManuscriptFileMeta) => a.path.localeCompare(b.path);
  roots.sort(byPath);
  const groups = [...dirs.entries()]
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([dir, list]) => ({ dir, files: list.sort(byPath) }));
  return { roots, groups };
}

export function FileTree({ files, currentId, busy, onSelect, onCreate, onRename, onDelete }: FileTreeProps) {
  const [adding, setAdding] = useState(false);
  const [newPath, setNewPath] = useState('');
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());
  const [renameTarget, setRenameTarget] = useState<ManuscriptFileMeta | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<ManuscriptFileMeta | null>(null);

  const { roots, groups } = useMemo(() => groupFiles(files), [files]);

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

  function toggleDir(dir: string) {
    setCollapsed((prev) => {
      const next = new Set(prev);
      if (next.has(dir)) next.delete(dir);
      else next.add(dir);
      return next;
    });
  }

  function FileRow({ f, indent }: { f: ManuscriptFileMeta; indent?: boolean }) {
    const active = f.id === currentId;
    // 目录内显示文件名，根文件显示完整路径
    const display = indent ? f.path.slice(f.path.indexOf('/') + 1) : f.path;
    return (
      <div
        className="row gap6 writer-file"
        onClick={() => onSelect(f)}
        title={f.path}
        style={{
          padding: '5px 8px',
          paddingLeft: indent ? 24 : 8,
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
          {display}
        </span>
        {f.readonly ? (
          <span title="只读文件（模板样式 / 自动生成）" style={{ color: 'var(--text-4)', display: 'flex' }}>
            <LockIcon />
          </span>
        ) : (
          <span className="row gap6 writer-file-ops" onClick={(e) => e.stopPropagation()}>
            <button
              className="writer-mini-btn"
              title="重命名"
              disabled={busy}
              onClick={() => setRenameTarget(f)}
            >
              <Icon name="pen" size={11} />
            </button>
            <button
              className="writer-mini-btn"
              title="删除"
              disabled={busy}
              onClick={() => setDeleteTarget(f)}
            >
              <Icon name="trash" size={11} />
            </button>
          </span>
        )}
      </div>
    );
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', minHeight: 0, height: '100%' }}>
      <div
        className="row"
        style={{ padding: '10px 12px 8px', justifyContent: 'space-between', flexShrink: 0 }}
      >
        <span style={{ fontSize: 11, fontWeight: 650, color: 'var(--text-3)', letterSpacing: '0.04em' }}>
          文件
        </span>
        <button
          className="icon-btn"
          style={{ width: 22, height: 22, borderRadius: 6 }}
          title="新建文件"
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
            placeholder="如 appendix.tex"
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
        {roots.length === 0 && groups.length === 0 && (
          <div style={{ fontSize: 11.5, color: 'var(--text-4)', padding: '8px 6px' }}>还没有文件</div>
        )}
        {groups.map(({ dir, files: dirFiles }) => {
          const isCollapsed = collapsed.has(dir);
          return (
            <div key={dir}>
              <div
                className="row gap6 writer-file"
                onClick={() => toggleDir(dir)}
                style={{ padding: '5px 8px', borderRadius: 7, cursor: 'pointer', color: 'var(--text-3)' }}
                title={isCollapsed ? '展开目录' : '收起目录'}
              >
                <Icon
                  name="chevDown"
                  size={10}
                  style={{ transform: isCollapsed ? 'rotate(-90deg)' : 'none', transition: 'transform .12s', flexShrink: 0 }}
                />
                <span style={{ display: 'flex', color: 'var(--text-4)' }}>
                  <FolderIcon />
                </span>
                <span className="mono" style={{ flex: 1, minWidth: 0, fontSize: 11.5, fontWeight: 600 }}>
                  {dir}/
                </span>
                <span className="mono" style={{ fontSize: 10, color: 'var(--text-4)' }}>{dirFiles.length}</span>
              </div>
              {!isCollapsed && dirFiles.map((f) => <FileRow key={f.id} f={f} indent />)}
            </div>
          );
        })}
        {roots.map((f) => (
          <FileRow key={f.id} f={f} />
        ))}
      </div>

      {/* —— 重命名 / 删除对话框 —— */}
      {renameTarget && (
        <PromptModal
          open
          onClose={() => setRenameTarget(null)}
          title="重命名文件"
          label={`当前路径：${renameTarget.path}`}
          placeholder="新文件名（含路径）"
          initial={renameTarget.path}
          submitText="重命名"
          mono
          busy={busy}
          onSubmit={(path) => {
            if (path !== renameTarget.path) onRename(renameTarget, path);
            setRenameTarget(null);
          }}
        />
      )}
      {deleteTarget && (
        <ConfirmModal
          open
          onClose={() => setDeleteTarget(null)}
          title="删除文件"
          message={
            <>
              确定删除 <span className="mono">{deleteTarget.path}</span>
              ？删除后无法恢复（版本历史也会一并删除）。
            </>
          }
          confirmText="删除"
          danger
          busy={busy}
          onConfirm={() => {
            onDelete(deleteTarget);
            setDeleteTarget(null);
          }}
        />
      )}
    </div>
  );
}
