import { useEffect, useRef, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { EditorState, Prec } from '@codemirror/state';
import { EditorView, keymap } from '@codemirror/view';
import { basicSetup } from 'codemirror';
import { search } from '@codemirror/search';
import { StreamLanguage } from '@codemirror/language';
import { stex } from '@codemirror/legacy-modes/mode/stex';
import * as Y from 'yjs';
import { yCollab, yUndoManagerKeymap } from 'y-codemirror.next';
import { api, getToken, type ManuscriptFileMeta } from '../../lib/api';
import { Icon } from '../../components/ui/Icon';
import { tr } from '../../lib/i18n';
import { ManuscriptProvider, type ProviderStatus } from '../../lib/yjs-provider';
import { EmptyState } from '../../components/ui/EmptyState';
import { aiCursorExtension, setAiTarget, type AiTarget } from './aiCursor';

/* ============================================================
   CodeMirror6 编辑面板：
   - 可写文件：yjs CRDT 协同（自写 provider + y-codemirror.next），
     awareness 多人光标，Cmd+S 触发编译
   - readonly 文件（.sty/.cls/.bst、references.bib、figures/…）：
     REST 拉内容只读展示
   字号/行高/配色全部取自 tokens。
   ============================================================ */

export interface PeerInfo {
  clientId: number;
  name: string;
  color: string;
}

/** 编辑器主题（颜色取 tokens，stex 高亮用 CodeMirror 默认 highlightStyle）。 */
const cmTheme = EditorView.theme({
  '&': {
    height: '100%',
    fontSize: '12.5px',
    backgroundColor: 'var(--surface)',
    color: 'var(--text)',
  },
  '&.cm-focused': { outline: 'none' },
  '.cm-scroller': {
    fontFamily: 'var(--mono)',
    lineHeight: '1.65',
    overflow: 'auto',
  },
  '.cm-content': { padding: '10px 0' },
  '.cm-gutters': {
    backgroundColor: 'var(--surface-2)',
    color: 'var(--text-4)',
    border: 'none',
    borderRight: '0.5px solid var(--border)',
  },
  // 当前行底色必须半透明：drawSelection 的选区层画在正文下方，不透明底色
  // 会盖住行内（部分字符）选区的高亮矩形，导致只有整行选区才可见。
  '.cm-activeLine': { backgroundColor: 'var(--cm-active-line)' },
  '.cm-activeLineGutter': {
    backgroundColor: 'var(--accent-soft)',
    color: 'var(--accent-text)',
  },
  // drawSelection() 画的选区层（.cm-selectionLayer > .cm-selectionBackground 矩形，
  // 能正确覆盖行内部分选区）：CodeMirror baseTheme 用更高优先级选择器把 focused
  // 选区强制成默认淡紫色，这里用 !important 盖回主题选区色，focused / blur 都覆盖。
  '.cm-selectionBackground, &.cm-focused .cm-selectionBackground, & .cm-selectionLayer .cm-selectionBackground':
    { background: 'var(--cm-selection) !important' },
  '.cm-cursor': { borderLeftColor: 'var(--accent)' },

  /* —— 搜索匹配高亮 —— */
  '.cm-searchMatch': { backgroundColor: 'var(--cm-search-match)', borderRadius: '2px' },
  '.cm-searchMatch.cm-searchMatch-selected': { backgroundColor: 'var(--cm-search-current)' },
  '.cm-selectionMatch': { backgroundColor: 'var(--cm-search-match)', borderRadius: '2px' },

  /* —— 查找/替换面板（@codemirror/search 默认面板 → 贴合应用风格） —— */
  '.cm-panels': {
    backgroundColor: 'var(--surface-2)',
    color: 'var(--text)',
    border: 'none',
  },
  '.cm-panels.cm-panels-top': { borderBottom: '0.5px solid var(--border)' },
  '.cm-panels.cm-panels-bottom': { borderTop: '0.5px solid var(--border)' },
  '.cm-panel.cm-search': {
    padding: '8px 12px',
    fontFamily: 'var(--sans, inherit)',
    fontSize: '12px',
    display: 'flex',
    flexWrap: 'wrap',
    alignItems: 'center',
    gap: '6px',
  },
  '.cm-panel.cm-search br': { display: 'none' },
  '.cm-panel.cm-search label': {
    display: 'inline-flex',
    alignItems: 'center',
    gap: '3px',
    fontSize: '11px',
    color: 'var(--text-2)',
    userSelect: 'none',
  },
  '.cm-panel.cm-search input[type=checkbox]': { accentColor: 'var(--accent)', margin: 0 },
  '.cm-panel.cm-search .cm-textfield': {
    backgroundColor: 'var(--surface)',
    color: 'var(--text)',
    border: '1px solid var(--border)',
    borderRadius: '6px',
    padding: '4px 8px',
    fontSize: '12px',
    minWidth: '160px',
    outline: 'none',
  },
  '.cm-panel.cm-search .cm-textfield:focus': {
    borderColor: 'var(--accent)',
    boxShadow: '0 0 0 2px var(--accent-soft)',
  },
  '.cm-panel.cm-search .cm-button': {
    backgroundColor: 'var(--surface-3)',
    backgroundImage: 'none',
    color: 'var(--text-2)',
    border: '1px solid var(--border)',
    borderRadius: '6px',
    padding: '4px 9px',
    fontSize: '11.5px',
    cursor: 'pointer',
  },
  '.cm-panel.cm-search .cm-button:hover': {
    backgroundColor: 'var(--accent-soft)',
    color: 'var(--accent-text)',
    borderColor: 'var(--accent-soft-2)',
  },
  '.cm-panel.cm-search [name=close]': {
    position: 'absolute',
    top: '4px',
    right: '8px',
    background: 'transparent',
    border: 'none',
    color: 'var(--text-3)',
    cursor: 'pointer',
    fontSize: '18px',
    lineHeight: 1,
    padding: '2px 6px',
  },
  '.cm-panel.cm-search [name=close]:hover': { color: 'var(--text)' },
});

/**
 * 查找/替换：search 面板置顶 + 面板文案本地化（tr 不能在模块顶层调用，
 * 因此包成函数，在建编辑器时求值）。搜索键位（⌘F）与命中高亮已含在 basicSetup 里。
 */
function searchExtensions() {
  return [
    search({ top: true }),
    EditorState.phrases.of({
      'Find': tr('查找', 'Find'),
      'Replace': tr('替换为', 'Replace'),
      'next': tr('下一个', 'Next'),
      'previous': tr('上一个', 'Prev'),
      'all': tr('全选', 'Select all'),
      'match case': tr('区分大小写', 'match case'),
      'regexp': tr('正则', 'regexp'),
      'by word': tr('全词', 'by word'),
      'replace': tr('替换', 'Replace'),
      'replace all': tr('全部替换', 'Replace all'),
      'close': tr('关闭', 'close'),
      'current match': tr('当前匹配', 'current match'),
      'on line': tr('位于第', 'on line'),
      'Go to line': tr('跳转到行', 'Go to line'),
      'go': tr('跳转', 'go'),
    }),
  ];
}

const baseExtensions = [basicSetup, StreamLanguage.define(stex), EditorView.lineWrapping, cmTheme];

export interface EditorPaneProps {
  manuscriptId: string;
  fileId: string;
  readonly: boolean;
  /** 本人 awareness 信息（display_name 回退 email 前缀 → hash 颜色）。 */
  user: { name: string; color: string };
  /** Cmd+S 触发。 */
  onCompile: () => void;
  onStatus: (s: ProviderStatus) => void;
  onPeers: (peers: PeerInfo[]) => void;
  /** view 就绪/销毁回调（诊断跳行用）。 */
  onView: (v: EditorView | null) => void;
  /** 文档内容变化（防抖后回调，大纲面板用）。 */
  onDocChange?: (content: string) => void;
  /** AI 起草时当前正在写的小节（画 AI 光标）；null = 无。 */
  aiTarget?: AiTarget | null;
}

export function EditorPane(props: EditorPaneProps) {
  // key 由父组件按 fileId 设置，切文件时整体重建
  return props.readonly ? <ReadonlyEditor {...props} /> : <CollabEditor {...props} />;
}

/* ---------------- 协同编辑（CRDT） ---------------- */

function CollabEditor({ manuscriptId: _mid, fileId, user, onCompile, onStatus, onPeers, onView, onDocChange, aiTarget }: EditorPaneProps) {
  const hostRef = useRef<HTMLDivElement | null>(null);
  const providerRef = useRef<ManuscriptProvider | null>(null);
  const viewRef = useRef<EditorView | null>(null);
  // 最新回调放 ref，避免父组件重渲染导致编辑器/连接重建
  const cbRef = useRef({ onCompile, onStatus, onPeers, onView, onDocChange });
  cbRef.current = { onCompile, onStatus, onPeers, onView, onDocChange };
  const userRef = useRef(user);
  userRef.current = user;

  useEffect(() => {
    const host = hostRef.current;
    if (!host) return;

    const provider = new ManuscriptProvider(fileId, getToken);
    providerRef.current = provider;
    const ytext = provider.doc.getText('content');
    const undoManager = new Y.UndoManager(ytext);

    const u = userRef.current;
    provider.awareness.setLocalStateField('user', {
      name: u.name,
      color: u.color,
      colorLight: `${u.color}33`,
    });

    const offStatus = provider.onStatus((s) => cbRef.current.onStatus(s));
    const emitPeers = () => {
      const peers: PeerInfo[] = [];
      provider.awareness.getStates().forEach((state, clientId) => {
        if (clientId === provider.doc.clientID) return;
        const pu = (state as { user?: { name?: string; color?: string } }).user;
        peers.push({
          clientId,
          name: pu?.name ?? tr('协作者', 'Collaborator'),
          color: pu?.color ?? '#8a94a8',
        });
      });
      cbRef.current.onPeers(peers);
    };
    provider.awareness.on('change', emitPeers);
    emitPeers();

    // 文档变化 → 防抖 400ms 通知父组件（大纲面板重算）
    let docTimer: ReturnType<typeof setTimeout> | null = null;
    const emitDoc = (text: string) => {
      if (docTimer) clearTimeout(docTimer);
      docTimer = setTimeout(() => cbRef.current.onDocChange?.(text), 400);
    };

    const state = EditorState.create({
      doc: ytext.toString(),
      extensions: [
        // Cmd/Ctrl+S → 编译（拦截浏览器保存）
        Prec.highest(
          keymap.of([
            {
              key: 'Mod-s',
              preventDefault: true,
              run: () => {
                cbRef.current.onCompile();
                return true;
              },
            },
          ]),
        ),
        keymap.of(yUndoManagerKeymap),
        ...baseExtensions,
        ...searchExtensions(),
        aiCursorExtension,
        yCollab(ytext, provider.awareness, { undoManager }),
        EditorView.updateListener.of((u) => {
          if (u.docChanged) emitDoc(u.state.doc.toString());
        }),
      ],
    });
    const view = new EditorView({ state, parent: host });
    viewRef.current = view;
    cbRef.current.onView(view);
    emitDoc(view.state.doc.toString());

    return () => {
      cbRef.current.onView(null);
      viewRef.current = null;
      if (docTimer) clearTimeout(docTimer);
      view.destroy();
      offStatus();
      provider.awareness.off('change', emitPeers);
      provider.destroy();
      provider.doc.destroy();
      providerRef.current = null;
    };
  }, [fileId]);

  // 用户信息就绪/变化时同步 awareness（不重建连接）
  useEffect(() => {
    providerRef.current?.awareness.setLocalStateField('user', {
      name: user.name,
      color: user.color,
      colorLight: `${user.color}33`,
    });
  }, [user.name, user.color]);

  // AI 起草目标变化 → 派发装饰效果（不重建编辑器）
  useEffect(() => {
    viewRef.current?.dispatch({ effects: setAiTarget.of(aiTarget ?? null) });
  }, [aiTarget?.section, aiTarget?.phase]);

  return <div ref={hostRef} style={{ flex: 1, minHeight: 0, minWidth: 0 }} />;
}

/* ---------------- 二进制文件只读预览（图片 / PDF / 其它） ---------------- */

const IMG_RE = /\.(png|jpe?g|gif|svg|webp|bmp|ico)$/i;

function fmtBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

/** 二进制文件（上传的图片/PDF 等）：不进 CRDT 编辑器，取原始字节做只读预览。 */
export function BinaryPreview({ manuscriptId, file }: { manuscriptId: string; file: ManuscriptFileMeta }) {
  const [url, setUrl] = useState<string | null>(null);
  const rawQuery = useQuery({
    queryKey: ['manuscript-file-raw', manuscriptId, file.id],
    queryFn: () => api.fetchManuscriptFileRaw(manuscriptId, file.id),
    retry: false,
    staleTime: Infinity,
  });
  const blob = rawQuery.data;

  useEffect(() => {
    if (!blob) {
      setUrl(null);
      return;
    }
    const u = URL.createObjectURL(blob);
    setUrl(u);
    return () => URL.revokeObjectURL(u);
  }, [blob]);

  const name = file.path.slice(file.path.lastIndexOf('/') + 1);
  const isImage = IMG_RE.test(file.path);

  if (rawQuery.isLoading) {
    return <div className="empty" style={{ flex: 1, paddingTop: 80 }}>{tr('加载文件…', 'Loading file…')}</div>;
  }
  if (rawQuery.isError || !url) {
    return (
      <div style={{ flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
        <EmptyState
          compact
          icon="x"
          title={tr('读不到这个文件', 'Cannot read this file')}
          desc={tr('后端不可用或文件已被删除。', 'Backend unavailable or the file was deleted.')}
          action={
            <button className="btn btn-soft sm" onClick={() => void rawQuery.refetch()}>
              {tr('重试', 'Retry')}
            </button>
          }
        />
      </div>
    );
  }

  if (isImage) {
    return (
      <div className="scroll" style={{ flex: 1, minHeight: 0, overflow: 'auto', display: 'flex', alignItems: 'center', justifyContent: 'center', padding: 20, background: 'var(--surface-2)' }}>
        <img src={url} alt={name} style={{ maxWidth: '100%', maxHeight: '100%', objectFit: 'contain', borderRadius: 6, boxShadow: '0 1px 8px rgba(0,0,0,0.12)' }} />
      </div>
    );
  }

  return (
    <div style={{ flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center', padding: 20 }}>
      <div style={{ textAlign: 'center', maxWidth: 340 }}>
        <div style={{ color: 'var(--text-4)', display: 'flex', justifyContent: 'center', marginBottom: 12 }}>
          <Icon name="file" size={40} />
        </div>
        <div className="mono" style={{ fontSize: 12.5, fontWeight: 600, wordBreak: 'break-all' }}>{name}</div>
        <div className="muted" style={{ fontSize: 11.5, marginTop: 4 }}>
          {tr('二进制文件，无法在线预览', 'Binary file — no inline preview')} · {fmtBytes(file.size)}
        </div>
        <a className="btn btn-primary sm" href={url} download={name} style={{ marginTop: 14, textDecoration: 'none' }}>
          <Icon name="download" size={13} />
          {tr('下载', 'Download')}
        </a>
      </div>
    </div>
  );
}

/* ---------------- 只读文件查看 ---------------- */

function ReadonlyEditor({ manuscriptId, fileId, onView, onDocChange }: EditorPaneProps) {
  const hostRef = useRef<HTMLDivElement | null>(null);
  const onViewRef = useRef(onView);
  onViewRef.current = onView;
  const onDocChangeRef = useRef(onDocChange);
  onDocChangeRef.current = onDocChange;

  const fileQuery = useQuery({
    queryKey: ['manuscript-file', manuscriptId, fileId],
    queryFn: () => api.getManuscriptFile(manuscriptId, fileId),
    retry: false,
    staleTime: 30_000,
  });
  const content = fileQuery.data?.content;

  useEffect(() => {
    const host = hostRef.current;
    if (!host || content === undefined) return;
    const view = new EditorView({
      state: EditorState.create({
        doc: content,
        extensions: [...baseExtensions, ...searchExtensions(), EditorState.readOnly.of(true), EditorView.editable.of(false)],
      }),
      parent: host,
    });
    onViewRef.current(view);
    onDocChangeRef.current?.(content);
    return () => {
      onViewRef.current(null);
      view.destroy();
    };
  }, [content]);

  if (fileQuery.isLoading) {
    return <div className="empty" style={{ flex: 1, paddingTop: 80 }}>{tr('加载文件内容…', 'Loading file…')}</div>;
  }
  if (fileQuery.isError) {
    return (
      <div style={{ flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
        <EmptyState
          compact
          icon="x"
          title={tr('读不到这个文件', 'Cannot read this file')}
          desc={tr('后端不可用或文件已被删除。', 'Backend unavailable or the file was deleted.')}
          action={
            <button className="btn btn-soft sm" onClick={() => void fileQuery.refetch()}>
              {tr('重试', 'Retry')}
            </button>
          }
        />
      </div>
    );
  }
  return <div ref={hostRef} style={{ flex: 1, minHeight: 0, minWidth: 0 }} />;
}
