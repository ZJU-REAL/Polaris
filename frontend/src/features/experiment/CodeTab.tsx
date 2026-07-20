import { useEffect, useMemo, useRef, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { EditorState } from '@codemirror/state';
import { EditorView } from '@codemirror/view';
import { basicSetup } from 'codemirror';
import { StreamLanguage } from '@codemirror/language';
import { python } from '@codemirror/legacy-modes/mode/python';
import { shell } from '@codemirror/legacy-modes/mode/shell';
import { json } from '@codemirror/legacy-modes/mode/javascript';
import { yaml } from '@codemirror/legacy-modes/mode/yaml';
import { api, type ExperimentCodeEntry, type ExperimentDetail } from '../../lib/api';
import { Icon } from '../../components/ui/Icon';
import { EmptyState } from '../../components/ui/EmptyState';
import { tr } from '../../lib/i18n';

/* ============================================================
   代码 Tab（VS Code 风格）：左侧文件树（目录可折叠）+ 右侧
   CodeMirror 只读查看器（行号 / 语法高亮 / 搜索），顶部当前
   文件路径。SSH 实时读 workdir，实验活动期间自动刷新。
   颜色全部取 tokens（暗色主题下即编辑器观感）。
   ============================================================ */

function fmtBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

/** 按扩展名挑语法高亮（legacy-modes 流式语法，包体已有依赖）。 */
function langExtensions(path: string) {
  if (path.endsWith('.py')) return [StreamLanguage.define(python)];
  if (path.endsWith('.sh') || path.endsWith('.bash')) return [StreamLanguage.define(shell)];
  if (path.endsWith('.json')) return [StreamLanguage.define(json)];
  if (path.endsWith('.yaml') || path.endsWith('.yml')) return [StreamLanguage.define(yaml)];
  if (path.endsWith('.txt') || path.endsWith('.log') || path.endsWith('.md')) return [];
  return [];
}

/* 查看器主题：颜色取 tokens；等宽、行号沟槽弱化（VS Code 观感） */
const viewerTheme = EditorView.theme({
  '&': {
    height: '100%',
    fontSize: '12.5px',
    backgroundColor: 'var(--surface)',
    color: 'var(--text)',
  },
  '.cm-content': { fontFamily: 'var(--mono)', padding: '10px 0' },
  '.cm-gutters': {
    backgroundColor: 'var(--surface-2)',
    color: 'var(--text-4)',
    border: 'none',
    fontFamily: 'var(--mono)',
    fontSize: '11px',
  },
  '.cm-activeLine': { backgroundColor: 'transparent' },
  '.cm-activeLineGutter': { backgroundColor: 'transparent' },
  '&.cm-focused': { outline: 'none' },
});

/** 只读 CodeMirror 查看器（每次内容/文件切换重建实例）。 */
function CodeViewer({ content, path }: { content: string; path: string }) {
  const hostRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const host = hostRef.current;
    if (!host) return;
    const view = new EditorView({
      state: EditorState.create({
        doc: content,
        extensions: [
          basicSetup,
          ...langExtensions(path),
          viewerTheme,
          EditorState.readOnly.of(true),
          EditorView.editable.of(false),
          EditorView.lineWrapping,
        ],
      }),
      parent: host,
    });
    return () => view.destroy();
  }, [content, path]);
  return <div ref={hostRef} style={{ height: '100%', overflow: 'hidden' }} />;
}

/* ---------------- 文件树 ---------------- */

interface TreeDir {
  name: string;
  path: string;
  dirs: Map<string, TreeDir>;
  files: ExperimentCodeEntry[];
}

function buildTree(files: ExperimentCodeEntry[]): TreeDir {
  const root: TreeDir = { name: '', path: '', dirs: new Map(), files: [] };
  for (const f of files) {
    const parts = f.path.split('/');
    let node = root;
    for (let i = 0; i < parts.length - 1; i++) {
      const seg = parts[i]!;
      const dirPath = parts.slice(0, i + 1).join('/');
      if (!node.dirs.has(seg)) node.dirs.set(seg, { name: seg, path: dirPath, dirs: new Map(), files: [] });
      node = node.dirs.get(seg)!;
    }
    node.files.push(f);
  }
  return root;
}

function TreeLevel({
  dir,
  depth,
  selected,
  collapsed,
  onToggle,
  onSelect,
}: {
  dir: TreeDir;
  depth: number;
  selected: string | null;
  collapsed: Set<string>;
  onToggle: (p: string) => void;
  onSelect: (p: string) => void;
}) {
  const dirs = [...dir.dirs.values()].sort((a, b) => a.name.localeCompare(b.name));
  const files = [...dir.files].sort((a, b) => a.path.localeCompare(b.path));
  return (
    <>
      {dirs.map((d) => {
        const closed = collapsed.has(d.path);
        return (
          <div key={d.path}>
            <button
              onClick={() => onToggle(d.path)}
              className="row gap6"
              style={{
                width: '100%',
                textAlign: 'left',
                border: 'none',
                cursor: 'pointer',
                background: 'transparent',
                color: 'var(--text-2)',
                padding: `4px 8px 4px ${8 + depth * 14}px`,
                alignItems: 'center',
                fontSize: 12.5,
                fontWeight: 600,
              }}
            >
              <Icon name="chevron" size={10} style={{ transform: closed ? 'rotate(-90deg)' : 'none', flexShrink: 0 }} />
              <Icon name="grid" size={12} style={{ color: 'var(--accent)', flexShrink: 0 }} />
              <span style={{ overflowWrap: 'anywhere' }}>{d.name}</span>
            </button>
            {!closed && (
              <TreeLevel dir={d} depth={depth + 1} selected={selected} collapsed={collapsed} onToggle={onToggle} onSelect={onSelect} />
            )}
          </div>
        );
      })}
      {files.map((f) => {
        const name = f.path.split('/').pop()!;
        const isSel = selected === f.path;
        return (
          <button
            key={f.path}
            onClick={() => onSelect(f.path)}
            className="row gap6"
            style={{
              width: '100%',
              textAlign: 'left',
              border: 'none',
              cursor: 'pointer',
              background: isSel ? 'var(--accent-soft)' : 'transparent',
              color: isSel ? 'var(--accent-text)' : 'var(--text-2)',
              padding: `4px 8px 4px ${22 + depth * 14}px`,
              alignItems: 'center',
              fontSize: 12.5,
            }}
          >
            <Icon name="file" size={12} style={{ flexShrink: 0 }} />
            <span className="mono" style={{ minWidth: 0, overflowWrap: 'anywhere' }}>{name}</span>
            <span className="mono muted" style={{ fontSize: 10, marginLeft: 'auto', flexShrink: 0 }}>{fmtBytes(f.size)}</span>
          </button>
        );
      })}
    </>
  );
}

/* ---------------- Tab 主体 ---------------- */

export function CodeTab({ exp, active }: { exp: ExperimentDetail; active: boolean }) {
  const [selected, setSelected] = useState<string | null>(null);
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());
  const listing = useQuery({
    queryKey: ['experiment', exp.id, 'code'],
    queryFn: () => api.getExperimentCode(exp.id),
    refetchInterval: active ? 15_000 : false,
  });
  const file = useQuery({
    queryKey: ['experiment', exp.id, 'code-file', selected],
    queryFn: () => api.getExperimentCodeFile(exp.id, selected!),
    enabled: selected != null,
    refetchInterval: active && selected != null ? 15_000 : false,
  });
  const files = listing.data?.files ?? [];
  const tree = useMemo(() => buildTree(files), [files]);

  // 首次加载后默认选中入口文件
  useEffect(() => {
    if (selected != null || files.length === 0) return;
    const prefer = ['run.sh', 'train.py', 'requirements.txt'];
    setSelected(prefer.find((p) => files.some((f) => f.path === p)) ?? files[0]!.path);
  }, [files, selected]);

  if (listing.isLoading) {
    return <div className="muted" style={{ padding: 40 }}>{tr('加载中…', 'Loading…')}</div>;
  }
  if (files.length === 0) {
    return (
      <EmptyState
        icon="git"
        title={tr('还没有代码', 'No code yet')}
        desc={tr('建环境步骤会生成实验代码并写入服务器工作目录。', 'The setup step generates experiment code into the remote workdir.')}
      />
    );
  }
  const live = listing.data?.source === 'ssh';
  return (
    <div className="fadeup">
      {/* 顶栏：来源徽标 + workdir + 刷新 */}
      <div className="row gap8" style={{ alignItems: 'center', marginBottom: 10, flexWrap: 'wrap' }}>
        <span
          className="pill sm"
          style={
            live
              ? { background: 'var(--accent-soft)', color: 'var(--accent-text)' }
              : { background: 'var(--surface-3)', color: 'var(--text-2)' }
          }
        >
          <Icon name={live ? 'play' : 'clock'} size={11} />
          {live ? tr('服务器实时', 'Live from server') : tr('离线快照', 'Offline snapshot')}
        </span>
        {listing.data?.workdir && (
          <span className="mono muted" style={{ fontSize: 11, wordBreak: 'break-all' }}>{listing.data.workdir}</span>
        )}
        <button
          className="btn btn-soft sm"
          onClick={() => { void listing.refetch(); void file.refetch(); }}
          style={{ marginLeft: 'auto' }}
        >
          <Icon name="refresh" size={12} /> {tr('刷新', 'Refresh')}
        </button>
      </div>

      {/* VS Code 式布局：左树右编辑器 */}
      <div
        className="card"
        style={{
          display: 'grid',
          gridTemplateColumns: '240px 1fr',
          overflow: 'hidden',
          height: 'calc(100vh - 290px)',
          minHeight: 420,
        }}
      >
        <div style={{ borderRight: '0.5px solid var(--border)', overflowY: 'auto', padding: '6px 0', background: 'var(--surface-2)' }}>
          <TreeLevel
            dir={tree}
            depth={0}
            selected={selected}
            collapsed={collapsed}
            onToggle={(p) =>
              setCollapsed((prev) => {
                const next = new Set(prev);
                if (next.has(p)) next.delete(p);
                else next.add(p);
                return next;
              })
            }
            onSelect={setSelected}
          />
        </div>
        <div style={{ display: 'flex', flexDirection: 'column', minWidth: 0 }}>
          {/* 当前文件“标签页” */}
          <div
            className="row gap6"
            style={{
              padding: '7px 14px',
              borderBottom: '0.5px solid var(--border)',
              alignItems: 'center',
              background: 'var(--surface-2)',
              flexShrink: 0,
            }}
          >
            <Icon name="file" size={12} style={{ color: 'var(--accent)' }} />
            <span className="mono" style={{ fontSize: 12, fontWeight: 600 }}>{selected ?? ''}</span>
            {file.data?.truncated && (
              <span className="pill sm" style={{ background: 'var(--surface-3)', color: 'var(--text-3)', marginLeft: 8 }}>
                {tr('仅前 200KB', 'first 200KB')}
              </span>
            )}
          </div>
          <div style={{ flex: 1, minHeight: 0 }}>
            {file.isLoading ? (
              <div className="muted" style={{ padding: 24 }}>{tr('加载中…', 'Loading…')}</div>
            ) : file.data ? (
              file.data.binary ? (
                <div className="muted" style={{ padding: 24, fontSize: 12.5 }}>
                  {tr('二进制文件，不支持预览', 'Binary file, preview unavailable')} · {fmtBytes(file.data.size)}
                </div>
              ) : (
                <CodeViewer content={file.data.content} path={file.data.path} />
              )
            ) : (
              <div className="muted" style={{ padding: 24 }}>{tr('选择左侧文件查看内容', 'Select a file to view')}</div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
