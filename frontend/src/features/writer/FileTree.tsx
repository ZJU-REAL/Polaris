import { useMemo, useRef, useState } from 'react';
import { Icon } from '../../components/ui/Icon';
import { ConfirmModal } from '../../components/ui/ConfirmModal';
import { PromptModal } from '../../components/ui/PromptModal';
import type { ManuscriptFileMeta } from '../../lib/api';
import { tr } from '../../lib/i18n';

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
  onCreateFolder: (path: string) => void;
  onUpload: (file: File) => void;
  onRename: (f: ManuscriptFileMeta, path: string) => void;
  onDelete: (f: ManuscriptFileMeta) => void;
}

const IMG_RE = /\.(png|jpe?g|gif|svg|webp|bmp|ico)$/i;

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

/** 图片文件图标（Icon 集里没有，就地画一个）。 */
function ImageIcon({ size = 12 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" style={{ display: 'block', flexShrink: 0 }}>
      <rect x="3" y="4" width="18" height="16" rx="2" fill="none" stroke="currentColor" strokeWidth="1.8" />
      <circle cx="8.5" cy="9" r="1.6" fill="currentColor" />
      <path d="M4 17l5-5 4 4 3-3 4 4" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

/** 按顶层目录分组：目录组在前（字典序），根文件在后（字典序）。
 *  is_folder 占位记录只用来登记目录（含空目录），不作为文件行重复显示。 */
function groupFiles(files: ManuscriptFileMeta[]) {
  const roots: ManuscriptFileMeta[] = [];
  const dirs = new Map<string, ManuscriptFileMeta[]>();
  const topSeg = (p: string) => {
    const norm = p.replace(/\/+$/, '');
    const slash = norm.indexOf('/');
    return slash === -1 ? norm : norm.slice(0, slash);
  };
  // 先登记文件夹占位（保证空目录也能显示）
  for (const f of files) {
    if (f.is_folder) {
      const dir = topSeg(f.path);
      if (dir && !dirs.has(dir)) dirs.set(dir, []);
    }
  }
  for (const f of files) {
    if (f.is_folder) continue;
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

export function FileTree({ files, currentId, busy, onSelect, onCreate, onCreateFolder, onUpload, onRename, onDelete }: FileTreeProps) {
  const [adding, setAdding] = useState(false);
  const [newPath, setNewPath] = useState('');
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());
  const [renameTarget, setRenameTarget] = useState<ManuscriptFileMeta | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<ManuscriptFileMeta | null>(null);
  const [folderPromptOpen, setFolderPromptOpen] = useState(false);
  const fileInputRef = useRef<HTMLInputElement | null>(null);

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
          {f.is_binary && IMG_RE.test(f.path) ? <ImageIcon /> : <Icon name="file" size={12} />}
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
          <span title={tr('只读文件（模板样式 / 自动生成）', 'Read-only file (template style / auto-generated)')} style={{ color: 'var(--text-4)', display: 'flex' }}>
            <LockIcon />
          </span>
        ) : (
          <span className="row gap6 writer-file-ops" onClick={(e) => e.stopPropagation()}>
            <button
              className="writer-mini-btn"
              title={tr('重命名', 'Rename')}
              disabled={busy}
              onClick={() => setRenameTarget(f)}
            >
              <Icon name="pen" size={11} />
            </button>
            <button
              className="writer-mini-btn"
              title={tr('删除', 'Delete')}
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
          {tr('文件', 'FILES')}
        </span>
        <span className="row gap6">
          <button
            className="icon-btn"
            style={{ width: 22, height: 22, borderRadius: 6 }}
            title={tr('新建文件', 'New file')}
            disabled={busy}
            onClick={() => setAdding((v) => !v)}
          >
            <Icon name="plus" size={12} />
          </button>
          <button
            className="icon-btn"
            style={{ width: 22, height: 22, borderRadius: 6 }}
            title={tr('新建文件夹', 'New folder')}
            disabled={busy}
            onClick={() => setFolderPromptOpen(true)}
          >
            <FolderIcon size={13} />
          </button>
          <button
            className="icon-btn"
            style={{ width: 22, height: 22, borderRadius: 6 }}
            title={tr('上传文件（含图片 / PDF）', 'Upload file (image / PDF)')}
            disabled={busy}
            onClick={() => fileInputRef.current?.click()}
          >
            <Icon name="download" size={12} style={{ transform: 'rotate(180deg)' }} />
          </button>
        </span>
        <input
          ref={fileInputRef}
          type="file"
          style={{ display: 'none' }}
          onChange={(e) => {
            const file = e.target.files?.[0];
            if (file) onUpload(file);
            e.target.value = '';
          }}
        />
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
        {roots.length === 0 && groups.length === 0 && (
          <div style={{ fontSize: 11.5, color: 'var(--text-4)', padding: '8px 6px' }}>{tr('还没有文件', 'No files yet')}</div>
        )}
        {groups.map(({ dir, files: dirFiles }) => {
          const isCollapsed = collapsed.has(dir);
          return (
            <div key={dir}>
              <div
                className="row gap6 writer-file"
                onClick={() => toggleDir(dir)}
                style={{ padding: '5px 8px', borderRadius: 7, cursor: 'pointer', color: 'var(--text-3)' }}
                title={isCollapsed ? tr('展开目录', 'Expand folder') : tr('收起目录', 'Collapse folder')}
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

      {/* —— 新建文件夹 / 重命名 / 删除对话框 —— */}
      <PromptModal
        open={folderPromptOpen}
        onClose={() => setFolderPromptOpen(false)}
        title={tr('新建文件夹', 'New folder')}
        label={tr('输入文件夹路径，例如 figures 或 sections/appendix', 'Enter a folder path, e.g. figures or sections/appendix')}
        placeholder={tr('如 figures', 'e.g. figures')}
        submitText={tr('创建', 'Create')}
        mono
        busy={busy}
        onSubmit={(path) => {
          onCreateFolder(path);
          setFolderPromptOpen(false);
        }}
      />
      {renameTarget && (
        <PromptModal
          open
          onClose={() => setRenameTarget(null)}
          title={tr('重命名文件', 'Rename file')}
          label={`${tr('当前路径：', 'Current path: ')}${renameTarget.path}`}
          placeholder={tr('新文件名（含路径）', 'New file name (with path)')}
          initial={renameTarget.path}
          submitText={tr('重命名', 'Rename')}
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
          title={tr('删除文件', 'Delete file')}
          message={
            <>
              {tr('确定删除', 'Delete')} <span className="mono">{deleteTarget.path}</span>
              {tr('？删除后无法恢复（版本历史也会一并删除）。', '? This cannot be undone (its version history is deleted too).')}
            </>
          }
          confirmText={tr('删除', 'Delete')}
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
