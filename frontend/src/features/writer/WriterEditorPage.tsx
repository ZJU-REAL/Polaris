import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Link, useNavigate, useParams, useSearchParams } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import type { EditorView } from '@codemirror/view';
import { Icon } from '../../components/ui/Icon';
import { StatusPill } from '../../components/ui/StatusPill';
import { EmptyState } from '../../components/ui/EmptyState';
import { ConfirmModal } from '../../components/ui/ConfirmModal';
import { PromptModal } from '../../components/ui/PromptModal';
import { toast } from '../../components/ui/Toast';
import {
  api,
  ApiError,
  type CompileResult,
  type DiagnosticItem,
  type ManuscriptFileMeta,
} from '../../lib/api';
import { fmtRelative } from '../../lib/format';
import type { ProviderStatus } from '../../lib/yjs-provider';
import { EditorPane, type PeerInfo } from './EditorPane';
import { FileTree } from './FileTree';
import { FactPackDrawer } from './FactPackDrawer';
import { DraftModal } from './DraftModal';
import { OutlinePanel } from './OutlinePanel';
import { AssistPanel, type AssistMode } from './AssistPanel';
import { HistoryModal } from './HistoryModal';
import { colorForUser, ruleText, sectionText, type AiWritingState } from './shared';

/* ============================================================
   /writer/:id — 论文编辑工作台（全屏三栏）：
   左 文件树 / 中 CodeMirror6 协同编辑（LaTeX 高亮 +
   多人光标 + ⌘S 编译）/ 右上下分栏（PDF 预览 + 编译诊断）。
   三栏之间可拖拽调宽（双击拖拽条恢复默认），左右两栏与
   PDF/诊断小节都可收起再展开，布局记在 localStorage。
   顶栏：面包屑标题、协作者在线点、事实包抽屉、AI 起草、
   编译、投稿。
   ============================================================ */

/* ---------------- PDF 预览 ---------------- */

function PdfPane({ msId, compile }: { msId: string; compile: CompileResult | null }) {
  const [url, setUrl] = useState<string | null>(null);

  const pdfQuery = useQuery({
    // compile.version 变化（编译成功后）自动重取
    queryKey: ['manuscript-pdf', msId, compile?.version ?? 0],
    queryFn: () => api.fetchManuscriptPdf(msId),
    enabled: !!compile,
    retry: false,
    staleTime: Infinity,
  });

  useEffect(() => {
    const blob = pdfQuery.data;
    if (!blob) {
      setUrl(null);
      return;
    }
    const typed = blob.type === 'application/pdf' ? blob : new Blob([blob], { type: 'application/pdf' });
    const u = URL.createObjectURL(typed);
    setUrl(u);
    return () => URL.revokeObjectURL(u);
  }, [pdfQuery.data]);

  if (!compile) {
    return (
      <div style={{ flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
        <EmptyState
          compact
          icon="file"
          title="还没有 PDF"
          desc="写好后点右上角的编译按钮或按 ⌘S，编译成功的 PDF 会出现在这里。"
        />
      </div>
    );
  }
  if (pdfQuery.isLoading) {
    return (
      <div style={{ flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
        <div style={{ textAlign: 'center' }}>
          <div className="pulse" style={{ width: 140, height: 190, margin: '0 auto 12px', borderRadius: 8, background: 'var(--surface-3)' }} />
          <div className="muted" style={{ fontSize: 12 }}>正在加载 PDF…</div>
        </div>
      </div>
    );
  }
  if (pdfQuery.isError || !url) {
    return (
      <div style={{ flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
        <EmptyState
          compact
          icon="file"
          title="还没有编译成功的 PDF"
          desc="最近一次编译没有产出 PDF，先按下方诊断把错误修掉，再编译一次。"
        />
      </div>
    );
  }
  return <iframe src={url} title="论文 PDF 预览" style={{ flex: 1, width: '100%', border: 'none', background: '#525659' }} />;
}

/* ---------------- 诊断面板 ---------------- */

function DiagRow({ d, onClick }: { d: DiagnosticItem; onClick: () => void }) {
  const isErr = d.severity === 'error';
  return (
    <div
      className="row gap8 writer-diag"
      onClick={onClick}
      style={{ padding: '6px 12px', cursor: 'pointer', alignItems: 'flex-start' }}
      title="点击跳到对应文件行"
    >
      <span
        style={{
          width: 15,
          height: 15,
          borderRadius: '50%',
          flexShrink: 0,
          marginTop: 1,
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          background: isErr ? 'var(--danger-bg)' : 'var(--warn-bg)',
          color: isErr ? 'var(--danger-tx)' : 'var(--warn-tx)',
          fontSize: 10,
          fontWeight: 800,
          lineHeight: 1,
        }}
      >
        {isErr ? '✕' : '!'}
      </span>
      <div style={{ flex: 1, minWidth: 0 }}>
        <div className="row gap8">
          <span className="mono" style={{ fontSize: 10.5, color: 'var(--accent-text)' }}>
            {d.file}
            {d.line != null ? `:${d.line}` : ''}
          </span>
          <span className="pill sm" style={{ height: 16, fontSize: 9.5, padding: '0 6px' }}>{ruleText(d.rule)}</span>
        </div>
        <div style={{ fontSize: 11.5, color: 'var(--text-2)', lineHeight: 1.5, marginTop: 2, overflowWrap: 'break-word' }}>
          {d.message}
        </div>
      </div>
    </div>
  );
}

/* ---------------- 可拖拽分栏 ---------------- */

/** 工作台布局：面板宽度/占比 + 各面板是否展开，整体存 localStorage。 */
interface WriterLayout {
  /** 左侧文件树宽度 px */
  leftW: number;
  /** 右侧预览栏宽度 px */
  rightW: number;
  /** PDF 在右栏中的高度占比（0~1，与诊断互补） */
  pdfFrac: number;
  leftOpen: boolean;
  rightOpen: boolean;
  pdfOpen: boolean;
  diagOpen: boolean;
  outlineOpen: boolean;
}

const LAYOUT_KEY = 'polaris-writer-layout';
const DEFAULT_LAYOUT: WriterLayout = {
  leftW: 220,
  rightW: 420,
  pdfFrac: 0.6,
  leftOpen: true,
  rightOpen: true,
  pdfOpen: true,
  diagOpen: true,
  outlineOpen: true,
};

function loadLayout(): WriterLayout {
  try {
    const raw = localStorage.getItem(LAYOUT_KEY);
    return raw ? { ...DEFAULT_LAYOUT, ...(JSON.parse(raw) as Partial<WriterLayout>) } : DEFAULT_LAYOUT;
  } catch {
    return DEFAULT_LAYOUT;
  }
}

const clamp = (v: number, lo: number, hi: number) => Math.min(hi, Math.max(lo, v));

/** 分栏拖拽条；collapsed 时只保留展开按钮，不可拖。 */
function Gutter({
  horizontal,
  dragging,
  onMouseDown,
  onReset,
  collapsed,
  onToggle,
  toggleTitle,
  chevronDeg,
}: {
  horizontal?: boolean;
  dragging?: boolean;
  onMouseDown?: (e: React.MouseEvent) => void;
  /** 双击恢复默认尺寸 */
  onReset?: () => void;
  collapsed?: boolean;
  onToggle?: () => void;
  toggleTitle?: string;
  /** 展开/收起箭头角度（基于右向 chevron 旋转） */
  chevronDeg?: number;
}) {
  const canDrag = !!onMouseDown && !collapsed;
  return (
    <div
      className={`panel-gutter ${horizontal ? 'h' : 'v'}${canDrag ? '' : ' no-drag'}${dragging ? ' dragging' : ''}`}
      onMouseDown={canDrag ? onMouseDown : undefined}
      onDoubleClick={collapsed ? undefined : onReset}
      title={canDrag ? '拖动调整宽度，双击恢复默认' : undefined}
    >
      {onToggle && (
        <button
          className={`panel-gutter-btn${collapsed ? ' always' : ''}`}
          title={toggleTitle}
          onMouseDown={(e) => e.stopPropagation()}
          onDoubleClick={(e) => e.stopPropagation()}
          onClick={onToggle}
        >
          <Icon name="chevron" size={10} style={{ transform: `rotate(${chevronDeg ?? 0}deg)` }} />
        </button>
      )}
    </div>
  );
}

/* ---------------- 页面 ---------------- */

export function WriterEditorPage() {
  const { id = '' } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  const detailQuery = useQuery({
    queryKey: ['manuscript', id],
    queryFn: () => api.getManuscript(id),
    enabled: !!id,
    retry: false,
    // 状态流转靠 WS manuscript.status 实时 invalidate（AppShell）；
    // 起草中仅留 30s 慢轮询兜底（WS 断线时不至于卡住）
    refetchInterval: (q) => (q.state.data?.status === 'writing' ? 30_000 : false),
  });
  const ms = detailQuery.data;

  // AI 起草实时相位（AppShell 从 WS manuscript.ai_writing 写入缓存；无网络请求）
  const { data: aiWriting } = useQuery<AiWritingState | null>({
    queryKey: ['ai-writing', id],
    enabled: false,
    initialData: null,
  });

  // —— 当前用户（awareness 用户名 + hash 颜色） ——
  const meQuery = useQuery({ queryKey: ['me'], queryFn: () => api.me(), retry: false, staleTime: 60_000 });
  const userName = meQuery.data?.display_name || meQuery.data?.email?.split('@')[0] || '我';
  const user = useMemo(() => ({ name: userName, color: colorForUser(userName) }), [userName]);

  // —— 当前文件 ——
  const [currentFileId, setCurrentFileId] = useState<string | null>(null);
  const currentFile: ManuscriptFileMeta | null = ms?.files.find((f) => f.id === currentFileId) ?? null;

  // 默认选中 main.tex（或第一个可写 .tex 文件）
  useEffect(() => {
    if (!ms || ms.files.length === 0) return;
    if (currentFileId && ms.files.some((f) => f.id === currentFileId)) return;
    const pick =
      ms.files.find((f) => !f.readonly && /(^|\/)main\.tex$/.test(f.path)) ??
      ms.files.find((f) => !f.readonly && f.path.endsWith('.tex')) ??
      ms.files.find((f) => !f.readonly) ??
      ms.files[0];
    setCurrentFileId(pick?.id ?? null);
  }, [ms, currentFileId]);

  // —— 协同连接状态 / 协作者 / 编辑器 view / 当前文件内容（大纲用） ——
  const [wsStatus, setWsStatus] = useState<ProviderStatus>('connecting');
  const [peers, setPeers] = useState<PeerInfo[]>([]);
  const [view, setView] = useState<EditorView | null>(null);
  const [docContent, setDocContent] = useState<string | null>(null);
  const handleStatus = useCallback((s: ProviderStatus) => setWsStatus(s), []);
  const handlePeers = useCallback((p: PeerInfo[]) => setPeers(p), []);
  const handleView = useCallback((v: EditorView | null) => setView(v), []);
  const handleDocChange = useCallback((c: string) => setDocContent(c), []);
  useEffect(() => {
    setWsStatus('connecting');
    setPeers([]);
    setDocContent(null);
  }, [currentFileId]);

  // —— 事实包引文/图表插入到编辑器光标处 ——
  const canInsert = !!view && !!currentFile && !currentFile.readonly;
  const insertSnippet = useCallback(
    (text: string) => {
      if (!view) return false;
      const sel = view.state.selection.main;
      view.dispatch({
        changes: { from: sel.from, to: sel.to, insert: text },
        selection: { anchor: sel.from + text.length },
        scrollIntoView: true,
      });
      view.focus();
      return true;
    },
    [view],
  );
  function onInsertCite(bibkey: string) {
    if (insertSnippet(`\\cite{${bibkey}}`)) toast(`已在光标处插入 \\cite{${bibkey}}`, 'ok');
  }
  // —— 内联 AI（润色/改写/续写）/ 版本历史 ——
  const [assistMode, setAssistMode] = useState<AssistMode | null>(null);
  const [historyOpen, setHistoryOpen] = useState(false);
  useEffect(() => {
    setAssistMode(null);
    setHistoryOpen(false);
  }, [currentFileId]);

  function onInsertFigure(figId: string, caption?: string | null) {
    const snippet = [
      '\\begin{figure}[t]',
      '  \\centering',
      `  \\includegraphics[width=\\linewidth]{figures/${figId}.pdf}`,
      `  \\caption{${caption ?? '（补充图注）'}}`,
      `  \\label{fig:${figId}}`,
      '\\end{figure}',
      '',
    ].join('\n');
    if (insertSnippet(snippet)) toast(`已在光标处插入图表 ${figId}`, 'ok');
  }

  // —— 编译 ——
  const compileMutation = useMutation({
    mutationFn: () => api.compileManuscript(id),
    onSuccess: (res) => {
      if (res.status === 'ok') {
        toast(`编译成功 · ${(res.duration_ms / 1000).toFixed(1)}s`, 'ok');
      } else if (res.status === 'timeout') {
        toast('编译超时（120 秒上限），试着精简文档后重试', 'error');
      } else {
        const errs = res.diagnostics.filter((d) => d.severity === 'error').length;
        toast(`编译没通过（${errs} 个错误），看右下角诊断`, 'error');
      }
      void queryClient.invalidateQueries({ queryKey: ['manuscript', id] });
      void queryClient.invalidateQueries({ queryKey: ['manuscripts'] });
    },
    onError: (e) => toast(`编译失败：${e instanceof Error ? e.message : String(e)}`, 'error'),
  });
  // ⌘S 快捷键走 ref，避免编辑器因 mutation 对象变化而重建
  const compileRef = useRef<() => void>(() => {});
  compileRef.current = () => {
    if (!compileMutation.isPending) compileMutation.mutate();
  };
  const handleCompileShortcut = useCallback(() => compileRef.current(), []);

  // 取「本次编译结果」与「详情里的最新编译」中更新的那个
  const compile = useMemo(() => {
    const a = compileMutation.data ?? null;
    const b = ms?.latest_compile ?? null;
    if (a && b) return a.version >= b.version ? a : b;
    return a ?? b;
  }, [compileMutation.data, ms?.latest_compile]);

  // —— 诊断点击跳转 ——
  const jumpToLine = useCallback((v: EditorView, line: number | null) => {
    if (line != null && line >= 1) {
      const ln = Math.min(v.state.doc.lines, line);
      const pos = v.state.doc.line(ln).from;
      v.dispatch({ selection: { anchor: pos }, scrollIntoView: true });
    }
    v.focus();
  }, []);
  const [pendingJump, setPendingJump] = useState<{ fileId: string; line: number | null } | null>(null);
  useEffect(() => {
    if (!view || !pendingJump || pendingJump.fileId !== currentFileId) return;
    // 稍等 CRDT 首次同步把内容灌进编辑器再跳行
    const t = setTimeout(() => {
      jumpToLine(view, pendingJump.line);
      setPendingJump(null);
    }, 400);
    return () => clearTimeout(t);
  }, [view, pendingJump, currentFileId, jumpToLine]);

  // —— ?goto=file.tex:42 深链（论文评审页查错清单跳入，消费后从 URL 移除） ——
  const [searchParams, setSearchParams] = useSearchParams();
  useEffect(() => {
    if (!ms) return;
    const g = searchParams.get('goto');
    if (!g) return;
    const m = /^(.+):(\d+)$/.exec(g);
    const rawFile = m ? m[1]! : g;
    const line = m ? Number(m[2]) : null;
    const norm = (p: string) => p.replace(/^\.\//, '').replace(/^\//, '');
    const target =
      ms.files.find((f) => norm(f.path) === norm(rawFile)) ??
      ms.files.find((f) => norm(f.path).endsWith(`/${norm(rawFile)}`));
    if (target) {
      setCurrentFileId(target.id);
      setPendingJump({ fileId: target.id, line });
    } else {
      toast(`稿件里没有找到文件 ${rawFile}`, 'info');
    }
    setSearchParams(
      (prev) => {
        const next = new URLSearchParams(prev);
        next.delete('goto');
        return next;
      },
      { replace: true },
    );
  }, [ms, searchParams, setSearchParams]);

  function onDiagClick(d: DiagnosticItem) {
    if (!ms) return;
    const norm = (p: string) => p.replace(/^\.\//, '').replace(/^\//, '');
    const target =
      ms.files.find((f) => norm(f.path) === norm(d.file)) ??
      ms.files.find((f) => norm(f.path).endsWith(`/${norm(d.file)}`));
    if (!target) {
      toast(`稿件里没有找到文件 ${d.file}`, 'info');
      return;
    }
    if (target.id === currentFileId && view) {
      jumpToLine(view, d.line);
    } else {
      setCurrentFileId(target.id);
      setPendingJump({ fileId: target.id, line: d.line });
    }
  }

  // —— 文件增删改名 ——
  const invalidateDetail = () => void queryClient.invalidateQueries({ queryKey: ['manuscript', id] });
  const createFileMutation = useMutation({
    mutationFn: (path: string) => api.createManuscriptFile(id, { path }),
    onSuccess: (f) => {
      invalidateDetail();
      setCurrentFileId(f.id);
    },
    onError: (e) => toast(`新建文件失败：${e instanceof Error ? e.message : String(e)}`, 'error'),
  });
  const renameFileMutation = useMutation({
    mutationFn: ({ fid, path }: { fid: string; path: string }) => api.renameManuscriptFile(id, fid, path),
    onSuccess: invalidateDetail,
    onError: (e) => toast(`重命名失败：${e instanceof Error ? e.message : String(e)}`, 'error'),
  });
  const deleteFileMutation = useMutation({
    mutationFn: (fid: string) => api.deleteManuscriptFile(id, fid),
    onSuccess: (_d, fid) => {
      if (fid === currentFileId) setCurrentFileId(null);
      invalidateDetail();
    },
    onError: (e) => toast(`删除失败：${e instanceof Error ? e.message : String(e)}`, 'error'),
  });
  const fileOpsBusy =
    createFileMutation.isPending || renameFileMutation.isPending || deleteFileMutation.isPending;

  // —— 投稿 ——
  const submitMutation = useMutation({
    mutationFn: () => api.submitManuscript(id),
    onSuccess: () => {
      toast('已提交投稿审批，批准后标记为已投稿', 'ok');
      void queryClient.invalidateQueries({ queryKey: ['manuscript', id] });
      void queryClient.invalidateQueries({ queryKey: ['manuscripts'] });
      void queryClient.invalidateQueries({ queryKey: ['gates'] });
    },
    onError: (e) => {
      if (e instanceof ApiError && e.status === 409 && e.message.includes('COMPILE_REQUIRED')) {
        toast('要先编译成功一次才能投稿', 'error');
      } else {
        toast(`投稿失败：${e instanceof Error ? e.message : String(e)}`, 'error');
      }
    },
  });
  const [submitConfirmOpen, setSubmitConfirmOpen] = useState(false);
  function onSubmit() {
    if (!compile || compile.status !== 'ok') {
      toast('要先编译成功一次才能投稿', 'error');
      return;
    }
    setSubmitConfirmOpen(true);
  }

  // —— 改标题 ——
  const [titleEditOpen, setTitleEditOpen] = useState(false);
  const titleMutation = useMutation({
    mutationFn: (title: string) => api.patchManuscript(id, { title }),
    onSuccess: () => {
      invalidateDetail();
      void queryClient.invalidateQueries({ queryKey: ['manuscripts'] });
    },
    onError: (e) => toast(`改标题失败：${e instanceof Error ? e.message : String(e)}`, 'error'),
  });

  // —— 抽屉 / Modal ——
  const [factOpen, setFactOpen] = useState(false);
  const [draftOpen, setDraftOpen] = useState(false);

  // —— 分栏布局：宽度/占比 + 展开状态，持久化到 localStorage ——
  const [layout, setLayout] = useState<WriterLayout>(loadLayout);
  const patchLayout = useCallback(
    (patch: Partial<WriterLayout>) => setLayout((l) => ({ ...l, ...patch })),
    [],
  );
  useEffect(() => {
    try {
      localStorage.setItem(LAYOUT_KEY, JSON.stringify(layout));
    } catch {
      /* 隐私模式等存不了就算了 */
    }
  }, [layout]);

  const splitRef = useRef<HTMLDivElement | null>(null); // 三栏容器（算右栏最大宽）
  const rightColRef = useRef<HTMLDivElement | null>(null); // 右栏（算 PDF 占比）
  const [dragging, setDragging] = useState<'left' | 'right' | 'pdf' | null>(null);

  const startDrag = useCallback(
    (e: React.MouseEvent, which: 'left' | 'right' | 'pdf', move: (dx: number, dy: number) => void) => {
      e.preventDefault();
      const sx = e.clientX;
      const sy = e.clientY;
      setDragging(which);
      document.body.style.userSelect = 'none';
      document.body.style.cursor = which === 'pdf' ? 'row-resize' : 'col-resize';
      const onMove = (ev: MouseEvent) => move(ev.clientX - sx, ev.clientY - sy);
      const onUp = () => {
        document.removeEventListener('mousemove', onMove);
        document.removeEventListener('mouseup', onUp);
        document.body.style.userSelect = '';
        document.body.style.cursor = '';
        setDragging(null);
      };
      document.addEventListener('mousemove', onMove);
      document.addEventListener('mouseup', onUp);
    },
    [],
  );

  function onLeftGutterDown(e: React.MouseEvent) {
    const start = layout.leftW;
    startDrag(e, 'left', (dx) => patchLayout({ leftW: clamp(start + dx, 140, 420) }));
  }
  function onRightGutterDown(e: React.MouseEvent) {
    const start = layout.rightW;
    const max = Math.max(320, (splitRef.current?.clientWidth ?? 1200) * 0.6);
    startDrag(e, 'right', (dx) => patchLayout({ rightW: clamp(start - dx, 260, max) }));
  }
  function onPdfGutterDown(e: React.MouseEvent) {
    const start = layout.pdfFrac;
    const h = rightColRef.current?.clientHeight || 600;
    startDrag(e, 'pdf', (_dx, dy) => patchLayout({ pdfFrac: clamp(start + dy / h, 0.15, 0.85) }));
  }

  /* ---------------- 渲染 ---------------- */

  if (detailQuery.isLoading) {
    return <div className="empty" style={{ marginTop: 120 }}>加载论文草稿…</div>;
  }
  if (detailQuery.isError || !ms) {
    return (
      <div style={{ marginTop: 100 }}>
        <EmptyState
          icon="x"
          title="打不开这篇论文草稿"
          desc="草稿不存在、你不在这个研究方向里，或后端暂时不可用。"
          action={
            <button className="btn btn-ghost" onClick={() => navigate('/writer')}>
              <Icon name="pen" size={14} />
              回论文列表
            </button>
          }
        />
      </div>
    );
  }

  const isWriting = ms.status === 'writing';
  // AI 正在写当前文件的哪一节（给编辑器画光标，仅撰写/修订相位）；跨文件时只在状态条给跳转
  const aiPenActive =
    isWriting && (aiWriting?.phase === 'typing' || aiWriting?.phase === 'revising');
  const aiTarget =
    aiPenActive && aiWriting!.fileId === currentFileId && aiWriting!.section
      ? { section: aiWriting!.section, phase: aiWriting!.phase as 'typing' | 'revising' }
      : null;
  const aiFile = aiWriting?.fileId ? ms.files.find((f) => f.id === aiWriting.fileId) : null;
  const showDisconnect = !!currentFile && !currentFile.readonly && wsStatus === 'disconnected';
  const errCount = compile?.diagnostics.filter((d) => d.severity === 'error').length ?? 0;
  const warnCount = compile?.diagnostics.filter((d) => d.severity === 'warning').length ?? 0;

  return (
    <div style={{ height: '100%', display: 'flex', flexDirection: 'column' }}>
      {/* —— 顶栏：面包屑 + 标题 + 操作 —— */}
      <div
        className="row gap10"
        style={{
          padding: '9px 16px',
          borderBottom: '0.5px solid var(--border)',
          background: 'var(--surface)',
          flexShrink: 0,
        }}
      >
        <button className="btn btn-ghost sm" onClick={() => navigate('/writer')}>
          <Icon name="chevron" size={13} style={{ transform: 'rotate(180deg)' }} />
          论文列表
        </button>
        <div className="row gap8" style={{ flex: 1, minWidth: 0 }}>
          <span
            title={ms.title}
            style={{
              fontSize: 13.5,
              fontWeight: 650,
              overflow: 'hidden',
              textOverflow: 'ellipsis',
              whiteSpace: 'nowrap',
              maxWidth: '40vw',
            }}
          >
            {ms.title}
          </span>
          <button
            className="writer-mini-btn"
            title="改标题"
            disabled={titleMutation.isPending}
            onClick={() => setTitleEditOpen(true)}
          >
            <Icon name="pen" size={11} />
          </button>
          <StatusPill status={ms.status} sm />
          {isWriting && ms.writing_voyage_id && (
            <Link
              to={`/voyages/${ms.writing_voyage_id}`}
              className="pill sm"
              style={{ background: 'var(--accent-soft)', color: 'var(--accent-text)', textDecoration: 'none' }}
            >
              <Icon name="sparkle" size={11} />
              AI 正在写，看任务进度
            </Link>
          )}
        </div>

        {/* 协作者在线点 */}
        {currentFile && !currentFile.readonly && (
          <div className="row" style={{ flexShrink: 0, marginRight: 2 }} title={peers.length > 0 ? `在线协作者：${peers.map((p) => p.name).join('、')}` : '当前只有你在编辑'}>
            <span
              style={{
                width: 7,
                height: 7,
                borderRadius: '50%',
                marginRight: 6,
                background:
                  wsStatus === 'connected' ? 'var(--ok)' : wsStatus === 'connecting' ? 'var(--warn)' : 'var(--danger)',
              }}
            />
            {peers.slice(0, 5).map((p) => (
              <span
                key={p.clientId}
                title={p.name}
                style={{
                  width: 21,
                  height: 21,
                  borderRadius: '50%',
                  background: p.color,
                  color: '#fff',
                  fontSize: 10,
                  fontWeight: 700,
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'center',
                  border: '1.5px solid var(--surface)',
                  marginLeft: -5,
                }}
              >
                {p.name.slice(0, 1).toUpperCase()}
              </span>
            ))}
            {peers.length > 5 && (
              <span className="mono" style={{ fontSize: 10, color: 'var(--text-3)', marginLeft: 4 }}>+{peers.length - 5}</span>
            )}
          </div>
        )}

        <button className="btn btn-ghost sm" onClick={() => setFactOpen(true)}>
          <Icon name="layers" size={13} />
          事实包
        </button>
        <button
          className="btn btn-ghost sm"
          disabled={isWriting}
          title={isWriting ? 'AI 正在起草中' : 'AI 按事实包起草各节'}
          onClick={() => setDraftOpen(true)}
        >
          <Icon name="sparkle" size={13} />
          AI 起草
        </button>
        <button
          className="btn btn-primary sm"
          disabled={compileMutation.isPending}
          title="⌘S 也可触发（tectonic，最长 120 秒）"
          onClick={() => compileMutation.mutate()}
        >
          {compileMutation.isPending ? (
            <>
              <Icon name="refresh" size={13} style={{ animation: 'spin 1s linear infinite' }} />
              编译中…
            </>
          ) : (
            <>
              <Icon name="play" size={13} />
              编译
            </>
          )}
        </button>
        <button
          className="btn btn-ghost sm"
          disabled={submitMutation.isPending || isWriting || ms.status === 'submitted'}
          title={ms.status === 'submitted' ? '已投稿' : '发起投稿审批'}
          onClick={onSubmit}
        >
          <Icon name="arrow" size={13} />
          {submitMutation.isPending ? '提交中…' : '投稿'}
        </button>
      </div>

      {/* —— 断线提示条 —— */}
      {showDisconnect && (
        <div
          className="row gap8"
          style={{
            background: 'var(--warn-bg)',
            color: 'var(--warn-tx)',
            fontSize: 12,
            padding: '6px 16px',
            flexShrink: 0,
          }}
        >
          <Icon name="bell" size={13} />
          连接断开，改动已暂存在本地，重连后会自动同步。正在重试…
        </div>
      )}

      {/* —— AI 起草实时状态条 —— */}
      {isWriting && aiWriting && (
        <div
          className="row gap8"
          style={{
            background: 'var(--accent-soft)',
            color: 'var(--accent-text)',
            fontSize: 12,
            padding: '6px 16px',
            flexShrink: 0,
          }}
        >
          <span className="ai-writing-dot" />
          {aiWriting.phase === 'compiling' ? (
            <span>AI 正在编译论文…</span>
          ) : aiWriting.phase === 'done' ? (
            <span>AI 起草中…</span>
          ) : (
            <span>
              AI 正在{aiWriting.phase === 'revising' ? '修订' : '撰写'}
              〈{aiWriting.section ? sectionText(aiWriting.section) : '正文'}〉…
            </span>
          )}
          {aiFile && aiWriting.fileId !== currentFileId && (
            <button
              className="btn btn-ghost sm"
              style={{ height: 22, fontSize: 10.5, padding: '0 8px', marginLeft: 4 }}
              onClick={() => setCurrentFileId(aiFile.id)}
            >
              到 {aiFile.path} 看 AI 撰写
            </button>
          )}
          <span style={{ flex: 1 }} />
          {ms.writing_voyage_id && (
            <Link
              to={`/voyages/${ms.writing_voyage_id}`}
              style={{ color: 'var(--accent-text)', textDecoration: 'underline', fontSize: 11 }}
            >
              看任务进度
            </Link>
          )}
        </div>
      )}

      {/* —— 三栏（可拖拽调宽 / 可收起） —— */}
      <div ref={splitRef} style={{ flex: 1, minHeight: 0, display: 'flex' }}>
        {/* 左：文件树 */}
        {layout.leftOpen && (
          <div
            style={{
              width: layout.leftW,
              flexShrink: 0,
              borderRight: '0.5px solid var(--border)',
              background: 'var(--sidebar-bg)',
              minHeight: 0,
              overflow: 'hidden',
              display: 'flex',
              flexDirection: 'column',
            }}
          >
            <div style={{ flex: 1, minHeight: 0, overflow: 'hidden' }}>
              <FileTree
                files={ms.files}
                currentId={currentFileId}
                busy={fileOpsBusy}
                onSelect={(f) => setCurrentFileId(f.id)}
                onCreate={(path) => createFileMutation.mutate(path)}
                onRename={(f, path) => renameFileMutation.mutate({ fid: f.id, path })}
                onDelete={(f) => deleteFileMutation.mutate(f.id)}
              />
            </div>
            <OutlinePanel
              content={docContent}
              open={layout.outlineOpen}
              onToggle={() => patchLayout({ outlineOpen: !layout.outlineOpen })}
              onJump={(line) => {
                if (view) jumpToLine(view, line);
              }}
            />
          </div>
        )}
        <Gutter
          dragging={dragging === 'left'}
          onMouseDown={layout.leftOpen ? onLeftGutterDown : undefined}
          onReset={() => patchLayout({ leftW: DEFAULT_LAYOUT.leftW })}
          collapsed={!layout.leftOpen}
          onToggle={() => patchLayout({ leftOpen: !layout.leftOpen })}
          toggleTitle={layout.leftOpen ? '收起文件列表' : '展开文件列表'}
          chevronDeg={layout.leftOpen ? 180 : 0}
        />

        {/* 中：编辑器 */}
        <div style={{ flex: 1, display: 'flex', flexDirection: 'column', minWidth: 0, minHeight: 0, background: 'var(--surface)' }}>
          {currentFile ? (
            <>
              <div
                className="row gap8"
                style={{ padding: '6px 14px', borderBottom: '0.5px solid var(--border)', flexShrink: 0 }}
              >
                <span className="mono" style={{ fontSize: 11, color: 'var(--text-3)' }}>{currentFile.path}</span>
                {currentFile.readonly && (
                  <span className="pill sm" style={{ height: 17, fontSize: 9.5 }}>只读</span>
                )}
                {!currentFile.readonly && (
                  <span className="row gap6" style={{ marginLeft: 4 }}>
                    {(['polish', 'rewrite', 'continue'] as const).map((m) => (
                      <button
                        key={m}
                        className="btn btn-ghost sm"
                        style={{ height: 22, fontSize: 10.5, padding: '0 8px' }}
                        disabled={!view || isWriting}
                        title={
                          m === 'continue'
                            ? 'AI 从光标处向后续写'
                            : `选中一段文字后 AI ${m === 'polish' ? '润色' : '按要求改写'}`
                        }
                        onClick={() => setAssistMode((cur) => (cur === m ? null : m))}
                      >
                        <Icon name="sparkle" size={11} />
                        {m === 'polish' ? '润色' : m === 'rewrite' ? '改写' : '续写'}
                      </button>
                    ))}
                  </span>
                )}
                <button
                  className="writer-mini-btn"
                  style={{ marginLeft: 'auto' }}
                  title="版本历史（AI 写入前 / 每次编译自动存档）"
                  onClick={() => setHistoryOpen(true)}
                >
                  <Icon name="clock" size={11} />
                </button>
                <span className="mono" style={{ fontSize: 10, color: 'var(--text-4)' }}>
                  ⌘S 编译
                </span>
              </div>
              <EditorPane
                key={currentFile.id}
                manuscriptId={ms.id}
                fileId={currentFile.id}
                readonly={!!currentFile.readonly}
                user={user}
                onCompile={handleCompileShortcut}
                onStatus={handleStatus}
                onPeers={handlePeers}
                onView={handleView}
                onDocChange={handleDocChange}
                aiTarget={aiTarget}
              />
              {assistMode && view && currentFile && !currentFile.readonly && (
                <AssistPanel
                  key={`${currentFile.id}-${assistMode}`}
                  manuscriptId={ms.id}
                  mode={assistMode}
                  view={view}
                  onClose={() => setAssistMode(null)}
                />
              )}
            </>
          ) : (
            <div style={{ flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
              <EmptyState compact icon="file" title="还没有文件" desc="点左侧 + 号新建一个 .tex 文件开始写作。" />
            </div>
          )}
        </div>

        <Gutter
          dragging={dragging === 'right'}
          onMouseDown={layout.rightOpen ? onRightGutterDown : undefined}
          onReset={() => patchLayout({ rightW: DEFAULT_LAYOUT.rightW })}
          collapsed={!layout.rightOpen}
          onToggle={() => patchLayout({ rightOpen: !layout.rightOpen })}
          toggleTitle={layout.rightOpen ? '收起预览面板' : '展开预览面板'}
          chevronDeg={layout.rightOpen ? 0 : 180}
        />

        {/* 右：PDF 预览 + 诊断 */}
        {layout.rightOpen && (
          <div
            ref={rightColRef}
            style={{
              width: layout.rightW,
              flexShrink: 0,
              borderLeft: '0.5px solid var(--border)',
              display: 'flex',
              flexDirection: 'column',
              minHeight: 0,
              overflow: 'hidden',
            }}
          >
            {/* 上：PDF */}
            <div
              style={{
                flex: layout.pdfOpen ? `${layout.pdfFrac} 1 0px` : '0 0 auto',
                minHeight: 0,
                display: 'flex',
                flexDirection: 'column',
                borderBottom: '0.5px solid var(--border)',
              }}
            >
              <div className="row gap8" style={{ padding: '6px 12px', flexShrink: 0, borderBottom: layout.pdfOpen ? '0.5px solid var(--border)' : 'none', background: 'var(--surface)' }}>
                <button
                  className="writer-mini-btn"
                  title={layout.pdfOpen ? '收起编译预览' : '展开编译预览'}
                  onClick={() => patchLayout({ pdfOpen: !layout.pdfOpen })}
                >
                  <Icon name="chevDown" size={11} style={{ transform: layout.pdfOpen ? 'none' : 'rotate(-90deg)', transition: 'transform .12s' }} />
                </button>
                <span style={{ fontSize: 11.5, fontWeight: 650, color: 'var(--text-2)' }}>编译预览 · PDF</span>
                {compile && (
                  <span className="mono" style={{ fontSize: 10, color: 'var(--text-4)', marginLeft: 'auto' }}>
                    v{compile.version} · {fmtRelative(compile.compiled_at)} · {(compile.duration_ms / 1000).toFixed(1)}s
                  </span>
                )}
              </div>
              {layout.pdfOpen && (
                // 拖动分栏时挡住 iframe，避免鼠标事件被它吞掉
                <div style={{ flex: 1, minHeight: 0, display: 'flex', flexDirection: 'column', pointerEvents: dragging ? 'none' : 'auto' }}>
                  <PdfPane msId={ms.id} compile={compile} />
                </div>
              )}
            </div>

            {layout.pdfOpen && layout.diagOpen && (
              <Gutter
                horizontal
                dragging={dragging === 'pdf'}
                onMouseDown={onPdfGutterDown}
                onReset={() => patchLayout({ pdfFrac: DEFAULT_LAYOUT.pdfFrac })}
              />
            )}

            {/* 下：诊断 */}
            <div
              style={{
                flex: layout.diagOpen ? `${1 - layout.pdfFrac} 1 0px` : '0 0 auto',
                minHeight: 0,
                display: 'flex',
                flexDirection: 'column',
                background: 'var(--surface)',
              }}
            >
              <div className="row gap8" style={{ padding: '6px 12px', flexShrink: 0, borderBottom: layout.diagOpen ? '0.5px solid var(--border)' : 'none' }}>
                <button
                  className="writer-mini-btn"
                  title={layout.diagOpen ? '收起编译诊断' : '展开编译诊断'}
                  onClick={() => patchLayout({ diagOpen: !layout.diagOpen })}
                >
                  <Icon name="chevDown" size={11} style={{ transform: layout.diagOpen ? 'none' : 'rotate(-90deg)', transition: 'transform .12s' }} />
                </button>
                <span style={{ fontSize: 11.5, fontWeight: 650, color: 'var(--text-2)' }}>编译诊断</span>
                {compile && (
                  <span className="mono" style={{ fontSize: 10.5, marginLeft: 'auto' }}>
                    {errCount > 0 && <span style={{ color: 'var(--danger-tx)', fontWeight: 700 }}>{errCount} 错误</span>}
                    {errCount > 0 && warnCount > 0 && <span style={{ color: 'var(--text-4)' }}> · </span>}
                    {warnCount > 0 && <span style={{ color: 'var(--warn-tx)', fontWeight: 700 }}>{warnCount} 警告</span>}
                    {errCount === 0 && warnCount === 0 && <span style={{ color: 'var(--ok-tx)' }}>没有问题 ✓</span>}
                  </span>
                )}
              </div>
              {layout.diagOpen && (
                <div className="scroll" style={{ flex: 1, overflowY: 'auto', padding: '4px 0' }}>
                  {!compile ? (
                    <div className="empty" style={{ padding: 24, fontSize: 12 }}>还没编译过，编译后这里显示错误和警告。</div>
                  ) : compile.diagnostics.length === 0 ? (
                    <div className="empty" style={{ padding: 24, fontSize: 12 }}>
                      {compile.status === 'ok' ? '编译干净，没有错误和警告 🎉' : '编译未产出诊断信息。'}
                    </div>
                  ) : (
                    compile.diagnostics.map((d, i) => <DiagRow key={i} d={d} onClick={() => onDiagClick(d)} />)
                  )}
                </div>
              )}
            </div>
          </div>
        )}
      </div>

      {/* —— 抽屉 / Modal —— */}
      <PromptModal
        open={titleEditOpen}
        onClose={() => setTitleEditOpen(false)}
        title="修改论文标题"
        initial={ms.title}
        placeholder="论文标题"
        submitText="保存"
        busy={titleMutation.isPending}
        onSubmit={(title) => {
          if (title !== ms.title) titleMutation.mutate(title);
          setTitleEditOpen(false);
        }}
      />
      <ConfirmModal
        open={submitConfirmOpen}
        onClose={() => setSubmitConfirmOpen(false)}
        title="发起投稿"
        message="会创建一条论文投稿审批，人工批准后状态变为已投稿。确认发起？"
        confirmText="发起投稿"
        busy={submitMutation.isPending}
        onConfirm={() => {
          submitMutation.mutate();
          setSubmitConfirmOpen(false);
        }}
      />
      {currentFile && (
        <HistoryModal
          open={historyOpen}
          onClose={() => setHistoryOpen(false)}
          manuscriptId={ms.id}
          file={currentFile}
        />
      )}
      <FactPackDrawer
        open={factOpen}
        onClose={() => setFactOpen(false)}
        manuscript={ms}
        canInsert={canInsert}
        onInsertCite={onInsertCite}
        onInsertFigure={onInsertFigure}
      />
      <DraftModal open={draftOpen} onClose={() => setDraftOpen(false)} manuscript={ms} />
    </div>
  );
}
