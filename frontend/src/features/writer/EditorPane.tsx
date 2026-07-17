import { useEffect, useRef } from 'react';
import { useQuery } from '@tanstack/react-query';
import { EditorState, Prec } from '@codemirror/state';
import { EditorView, keymap } from '@codemirror/view';
import { basicSetup } from 'codemirror';
import { StreamLanguage } from '@codemirror/language';
import { stex } from '@codemirror/legacy-modes/mode/stex';
import * as Y from 'yjs';
import { yCollab, yUndoManagerKeymap } from 'y-codemirror.next';
import { api, getToken } from '../../lib/api';
import { tr } from '../../lib/i18n';
import { ManuscriptProvider, type ProviderStatus } from '../../lib/yjs-provider';
import { EmptyState } from '../../components/ui/EmptyState';

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
  '.cm-activeLine': { backgroundColor: 'var(--accent-soft-row)' },
  '.cm-activeLineGutter': {
    backgroundColor: 'var(--accent-soft)',
    color: 'var(--accent-text)',
  },
  '.cm-selectionBackground, &.cm-focused .cm-selectionBackground': {
    backgroundColor: 'var(--accent-soft-2)',
  },
  '.cm-cursor': { borderLeftColor: 'var(--accent)' },
});

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
}

export function EditorPane(props: EditorPaneProps) {
  // key 由父组件按 fileId 设置，切文件时整体重建
  return props.readonly ? <ReadonlyEditor {...props} /> : <CollabEditor {...props} />;
}

/* ---------------- 协同编辑（CRDT） ---------------- */

function CollabEditor({ manuscriptId: _mid, fileId, user, onCompile, onStatus, onPeers, onView }: EditorPaneProps) {
  const hostRef = useRef<HTMLDivElement | null>(null);
  const providerRef = useRef<ManuscriptProvider | null>(null);
  // 最新回调放 ref，避免父组件重渲染导致编辑器/连接重建
  const cbRef = useRef({ onCompile, onStatus, onPeers, onView });
  cbRef.current = { onCompile, onStatus, onPeers, onView };
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
        yCollab(ytext, provider.awareness, { undoManager }),
      ],
    });
    const view = new EditorView({ state, parent: host });
    cbRef.current.onView(view);

    return () => {
      cbRef.current.onView(null);
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

  return <div ref={hostRef} style={{ flex: 1, minHeight: 0, minWidth: 0 }} />;
}

/* ---------------- 只读文件查看 ---------------- */

function ReadonlyEditor({ manuscriptId, fileId, onView }: EditorPaneProps) {
  const hostRef = useRef<HTMLDivElement | null>(null);
  const onViewRef = useRef(onView);
  onViewRef.current = onView;

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
        extensions: [...baseExtensions, EditorState.readOnly.of(true), EditorView.editable.of(false)],
      }),
      parent: host,
    });
    onViewRef.current(view);
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
