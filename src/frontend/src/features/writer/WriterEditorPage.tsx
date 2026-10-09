import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Link, useNavigate, useParams, useSearchParams } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import type { EditorView } from '@codemirror/view';
import { openSearchPanel } from '@codemirror/search';
import { Icon } from '../../components/ui/Icon';
import { StatusPill } from '../../components/ui/StatusPill';
import { EmptyState } from '../../components/ui/EmptyState';
import { ConfirmModal } from '../../components/ui/ConfirmModal';
import { PromptModal } from '../../components/ui/PromptModal';
import { toast } from '../../components/ui/Toast';
import {
  api,
  ApiError,
  type CompileEngine,
  type CompileResult,
  type DiagnosticItem,
  type ManuscriptFileMeta,
  type ManuscriptFileRead,
} from '../../lib/api';
import { fmtRelative } from '../../lib/format';
import { tr } from '../../lib/i18n';
import { topicPath, useProject } from '../../app/project';
import { useIsCompact, useIsMobile } from '../../lib/useBreakpoint';
import type { ProviderStatus } from '../../lib/yjs-provider';
import { EditorPane, BinaryPreview, type PeerInfo } from './EditorPane';
import { FileTree } from './FileTree';
import { FactPackDrawer } from './FactPackDrawer';
import { DraftModal } from './DraftModal';
import { OutlinePanel } from './OutlinePanel';
import { AssistPanel, type AssistMode } from './AssistPanel';
import { HistoryModal } from './HistoryModal';
import { MoreMenu } from './MoreMenu';
import { AiDisclosureModal } from '../../components/ui/AiDisclosureModal';
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
          title={tr('还没有 PDF', 'No PDF yet')}
          desc={tr('点击「编译」或按 ⌘S 生成。', 'Click Compile or press ⌘S.')}
        />
      </div>
    );
  }
  if (pdfQuery.isLoading) {
    return (
      <div style={{ flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
        <div style={{ textAlign: 'center' }}>
          <div className="pulse" style={{ width: 140, height: 190, margin: '0 auto 12px', borderRadius: 8, background: 'var(--surface-3)' }} />
          <div className="muted" style={{ fontSize: 12 }}>{tr('正在加载 PDF…', 'Loading PDF…')}</div>
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
          title={tr('上次编译没有生成 PDF', 'The last compile produced no PDF')}
          desc={tr('按下方「编译问题」修正后重新编译。', 'Fix the compile issues below, then compile again.')}
        />
      </div>
    );
  }
  return <iframe src={url} title={tr('PDF 预览', 'PDF preview')} style={{ flex: 1, width: '100%', border: 'none', background: '#525659' }} />;
}

/* ---------------- 诊断面板 ---------------- */

function DiagRow({ d, onClick }: { d: DiagnosticItem; onClick: () => void }) {
  const isErr = d.severity === 'error';
  return (
    <div
      className="row gap8 writer-diag"
      onClick={onClick}
      style={{ padding: '6px 12px', cursor: 'pointer', alignItems: 'flex-start' }}
      title={tr('跳到这一行', 'Go to this line')}
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
          fontSize: 11,
          fontWeight: 600,
          lineHeight: 1,
        }}
      >
        {isErr ? '✕' : '!'}
      </span>
      <div style={{ flex: 1, minWidth: 0 }}>
        <div className="row gap8">
          <span className="mono" style={{ fontSize: 11, color: 'var(--accent-text)' }}>
            {d.file}
            {d.line != null ? `:${d.line}` : ''}
          </span>
          <span className="pill sm" style={{ height: 16, fontSize: 11, padding: '0 6px' }}>{ruleText(d.rule)}</span>
        </div>
        <div style={{ fontSize: 12, color: 'var(--text-2)', lineHeight: 1.5, marginTop: 2, overflowWrap: 'break-word' }}>
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
      title={canDrag ? tr('拖动调整宽度，双击恢复默认', 'Drag to resize, double-click to reset') : undefined}
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
  const { currentProjectId } = useProject();

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
  const userName = meQuery.data?.display_name || meQuery.data?.email?.split('@')[0] || tr('我', 'Me');
  const user = useMemo(() => ({ name: userName, color: colorForUser(userName) }), [userName]);

  // —— 当前文件 ——
  const [currentFileId, setCurrentFileId] = useState<string | null>(null);
  const currentFile: ManuscriptFileMeta | null = ms?.files.find((f) => f.id === currentFileId) ?? null;

  // 默认选中 main.tex（或第一个可写 .tex 文件）
  useEffect(() => {
    if (!ms || ms.files.length === 0) return;
    if (currentFileId && ms.files.some((f) => f.id === currentFileId)) return;
    const selectable = ms.files.filter((f) => !f.is_folder);
    const pick =
      selectable.find((f) => !f.readonly && /(^|\/)main\.tex$/.test(f.path)) ??
      selectable.find((f) => !f.readonly && f.path.endsWith('.tex')) ??
      selectable.find((f) => !f.readonly && !f.is_binary) ??
      selectable.find((f) => !f.readonly) ??
      selectable[0];
    setCurrentFileId(pick?.id ?? null);
  }, [ms, currentFileId]);

  // —— 协同连接状态 / 协作者 / 编辑器 view / 当前文件内容（大纲用） ——
  const [wsStatus, setWsStatus] = useState<ProviderStatus>('connecting');
  const [, setPeers] = useState<PeerInfo[]>([]);
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
  const canInsert = !!view && !!currentFile && !currentFile.readonly && !currentFile.is_binary;
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
    if (insertSnippet(`\\cite{${bibkey}}`)) toast(tr(`已插入 \\cite{${bibkey}}`, `Inserted \\cite{${bibkey}}`), 'ok');
  }
  // —— 内联 AI（润色/改写/续写）/ 版本历史 ——
  const [assistMode, setAssistMode] = useState<AssistMode | null>(null);
  const [historyOpen, setHistoryOpen] = useState(false);

  // 当前撰写用的模型名（内联 AI 与 AI 起草都跑在 writing 阶段；
  // 取模型路由表里 writing 阶段的 model，缺省回落到 default 阶段）。
  // 路由表是 admin 端点，非管理员会取不到 → retry:false + 优雅回落。
  const routesQuery = useQuery({
    queryKey: ['llm', 'routes'],
    queryFn: () => api.getLlmRoutes(),
    retry: false,
    staleTime: 5 * 60_000,
  });
  const writingModel = useMemo(() => {
    const routes = routesQuery.data;
    if (!routes) return null;
    const r = routes.find((x) => x.stage === 'writing') ?? routes.find((x) => x.stage === 'default');
    return r?.model?.trim() || null;
  }, [routesQuery.data]);
  useEffect(() => {
    setAssistMode(null);
    setHistoryOpen(false);
  }, [currentFileId]);

  function onInsertFigure(figId: string, caption?: string | null) {
    const snippet = [
      '\\begin{figure}[t]',
      '  \\centering',
      `  \\includegraphics[width=\\linewidth]{figures/${figId}.pdf}`,
      `  \\caption{${caption ?? tr('（补充图注）', '(add a caption)')}}`,
      `  \\label{fig:${figId}}`,
      '\\end{figure}',
      '',
    ].join('\n');
    if (insertSnippet(snippet)) toast(tr(`已插入图表 ${figId}`, `Inserted figure ${figId}`), 'ok');
  }

  // —— 刷新参考文献（引用池 → references.bib → 主 tex 接线） ——
  const refreshRefsMutation = useMutation({
    mutationFn: () => api.refreshReferences(id),
    onSuccess: (res) => {
      const wiring = res.bibliography_updated
        ? tr('，并修正了主文件的 \\bibliography', '; also fixed \\bibliography in the main file')
        : '';
      toast(
        tr(
          `已更新参考文献，共 ${res.entries} 条${wiring}`,
          `Updated references: ${res.entries} ${res.entries === 1 ? 'entry' : 'entries'}${wiring}`,
        ),
        'ok',
      );
      void queryClient.invalidateQueries({ queryKey: ['manuscript', id] });
    },
    onError: (e) =>
      toast(`${tr('无法更新参考文献：', 'Couldn’t update references: ')}${e instanceof Error ? e.message : String(e)}`, 'error'),
  });

  // —— 编译 ——
  const compileMutation = useMutation({
    mutationFn: () => api.compileManuscript(id),
    onSuccess: (res) => {
      if (res.status === 'ok') {
        const secs = (res.duration_ms / 1000).toFixed(1);
        toast(tr(`编译成功，用时 ${secs} 秒`, `Compiled in ${secs} s`), 'ok');
      } else if (res.status === 'timeout') {
        toast(tr('编译超过 120 秒上限，请精简文档后重试', 'Compile took longer than 120 s. Simplify the document and retry.'), 'error');
      } else {
        const errs = res.diagnostics.filter((d) => d.severity === 'error').length;
        toast(tr(`编译未通过：${errs} 个错误，见右下方「编译问题」`, `Compile failed with ${errs} ${errs === 1 ? 'error' : 'errors'}. See Compile issues.`), 'error');
      }
      void queryClient.invalidateQueries({ queryKey: ['manuscript', id] });
      void queryClient.invalidateQueries({ queryKey: ['manuscripts'] });
    },
    onError: (e) => toast(`${tr('无法编译：', 'Couldn’t compile: ')}${e instanceof Error ? e.message : String(e)}`, 'error'),
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
      toast(tr(`稿件中没有文件 ${rawFile}`, `No file named ${rawFile} in this manuscript`), 'info');
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
      toast(tr(`稿件中没有文件 ${d.file}`, `No file named ${d.file} in this manuscript`), 'info');
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
    onError: (e) => toast(`${tr('无法新建文件：', 'Couldn’t create the file: ')}${e instanceof Error ? e.message : String(e)}`, 'error'),
  });
  const renameFileMutation = useMutation({
    mutationFn: ({ fid, path }: { fid: string; path: string }) => api.renameManuscriptFile(id, fid, path),
    onSuccess: invalidateDetail,
    onError: (e) => toast(`${tr('无法重命名：', 'Couldn’t rename: ')}${e instanceof Error ? e.message : String(e)}`, 'error'),
  });
  const deleteFileMutation = useMutation({
    mutationFn: (fid: string) => api.deleteManuscriptFile(id, fid),
    onSuccess: (_d, fid) => {
      if (fid === currentFileId) setCurrentFileId(null);
      invalidateDetail();
    },
    onError: (e) => toast(`${tr('无法删除：', 'Couldn’t delete: ')}${e instanceof Error ? e.message : String(e)}`, 'error'),
  });
  const createFolderMutation = useMutation({
    mutationFn: (path: string) => api.createManuscriptFolder(id, path),
    onSuccess: invalidateDetail,
    onError: (e) => toast(`${tr('无法新建文件夹：', 'Couldn’t create the folder: ')}${e instanceof Error ? e.message : String(e)}`, 'error'),
  });
  const uploadFileMutation = useMutation({
    mutationFn: (file: File) => api.uploadManuscriptFile(id, file),
    onSuccess: (f) => {
      invalidateDetail();
      setCurrentFileId(f.id);
      toast(tr('已上传文件', 'File uploaded'), 'ok');
    },
    onError: (e) => toast(`${tr('上传失败：', 'Couldn’t upload: ')}${e instanceof Error ? e.message : String(e)}`, 'error'),
  });
  const fileOpsBusy =
    createFileMutation.isPending ||
    renameFileMutation.isPending ||
    deleteFileMutation.isPending ||
    createFolderMutation.isPending ||
    uploadFileMutation.isPending;

  // —— arXiv 清洁包导出 ——
  const exportArxivMutation = useMutation({
    mutationFn: () => api.exportManuscriptArxiv(id),
    onSuccess: ({ blob, notes }) => {
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `${(ms?.title || 'manuscript').replace(/[/\\?%*:|"<>]/g, '_')}-arxiv.tar.gz`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 2000);
      if (notes.length > 0) toast(`${tr('导出提示：', 'Export notes: ')}${notes.join('；')}`, 'info');
      else toast(tr('已导出 arXiv 投稿包', 'Exported arXiv package'), 'ok');
    },
    onError: (e) => toast(`${tr('导出失败：', 'Couldn’t export: ')}${e instanceof Error ? e.message : String(e)}`, 'error'),
  });

  // —— 投稿 ——
  const submitMutation = useMutation({
    mutationFn: () => api.submitManuscript(id),
    onSuccess: () => {
      toast(tr('已提交投稿审批', 'Sent for submission approval'), 'ok');
      void queryClient.invalidateQueries({ queryKey: ['manuscript', id] });
      void queryClient.invalidateQueries({ queryKey: ['manuscripts'] });
      void queryClient.invalidateQueries({ queryKey: ['gates'] });
    },
    onError: (e) => {
      if (e instanceof ApiError && e.status === 409 && e.message.includes('COMPILE_REQUIRED')) {
        toast(tr('请先成功编译一次再投稿', 'Compile successfully before submitting'), 'error');
      } else {
        toast(`${tr('无法投稿：', 'Couldn’t submit: ')}${e instanceof Error ? e.message : String(e)}`, 'error');
      }
    },
  });
  const [submitConfirmOpen, setSubmitConfirmOpen] = useState(false);
  function onSubmit() {
    if (!compile || compile.status !== 'ok') {
      toast(tr('请先成功编译一次再投稿', 'Compile successfully before submitting'), 'error');
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
    onError: (e) => toast(`${tr('无法修改标题：', 'Couldn’t change the title: ')}${e instanceof Error ? e.message : String(e)}`, 'error'),
  });

  // —— 主文件 / 编译器（Overleaf 式，存在 manuscript 上，编译时后端自取） ——
  const settingsMutation = useMutation({
    mutationFn: (input: { main_tex?: string; engine?: CompileEngine }) => api.patchManuscript(id, input),
    onSuccess: () => {
      invalidateDetail();
      void queryClient.invalidateQueries({ queryKey: ['manuscripts'] });
    },
    onError: (e) => {
      if (e instanceof ApiError && e.status === 422 && e.message.includes('MAIN_TEX_NOT_FOUND')) {
        toast(tr('找不到这个主文件，请换一个 .tex 文件', 'That main file no longer exists. Choose another .tex file.'), 'error');
      } else {
        toast(`${tr('保存失败：', 'Couldn’t save: ')}${e instanceof Error ? e.message : String(e)}`, 'error');
      }
    },
  });
  const texFiles = useMemo(
    () => ms?.files.filter((f) => !f.is_folder && f.path.endsWith('.tex')) ?? [],
    [ms?.files],
  );

  // —— 抽屉 / Modal ——
  const [factOpen, setFactOpen] = useState(false);
  const [disclosureOpen, setDisclosureOpen] = useState(false);
  const [draftOpen, setDraftOpen] = useState(false);

  // 「初始化结构」后打开新生成的 draft.tex：先等详情刷新（文件树里出现 draft.tex、
  // 主文件选择器切到 draft.tex），再选中它，避免自动选主文件的副作用把选区抢回去。
  const handleInitialized = useCallback(
    async (file: ManuscriptFileRead) => {
      await queryClient.invalidateQueries({ queryKey: ['manuscript', id] });
      setCurrentFileId(file.id);
    },
    [id, queryClient],
  );

  // —— 分栏布局：宽度/占比 + 展开状态，持久化到 localStorage ——
  const [layout, setLayout] = useState<WriterLayout>(loadLayout);
  // 窄窗口（平板 / 小笔记本，手机另走整体横滚）：三栏放不下，预览栏默认收起、
  // 文件树收窄，只在本次会话里展开，不覆盖宽屏下记住的布局
  const isCompact = useIsCompact();
  const isMobile = useIsMobile();
  const narrow = isCompact && !isMobile;
  const [narrowRightOpen, setNarrowRightOpen] = useState(false);
  const rightOpen = narrow ? narrowRightOpen : layout.rightOpen;
  const leftW = narrow ? Math.min(layout.leftW, 180) : layout.leftW;
  const toggleRight = () => (narrow ? setNarrowRightOpen((o) => !o) : patchLayout({ rightOpen: !layout.rightOpen }));
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
    return <div className="empty" style={{ marginTop: 120 }}>{tr('加载中…', 'Loading…')}</div>;
  }
  if (detailQuery.isError || !ms) {
    return (
      <div style={{ marginTop: 100 }}>
        <EmptyState
          icon="x"
          title={tr('无法打开这篇稿件', 'Couldn’t open this manuscript')}
          desc={tr('稿件可能已被删除，或本机引擎没有运行。', 'It may have been deleted, or the local engine isn’t running.')}
          action={
            <button className="btn btn-ghost" onClick={() => navigate(topicPath(currentProjectId, 'writer'))}>
              <Icon name="pen" size={14} />
              {tr('返回稿件列表', 'Back to manuscripts')}
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
  const showDisconnect = !!currentFile && !currentFile.readonly && !currentFile.is_binary && wsStatus === 'disconnected';
  const errCount = compile?.diagnostics.filter((d) => d.severity === 'error').length ?? 0;
  const warnCount = compile?.diagnostics.filter((d) => d.severity === 'warning').length ?? 0;

  return (
    <div style={{ height: '100%', display: 'flex', flexDirection: 'column' }}>
      {/* —— 顶栏：面包屑 + 标题 + 操作 —— */}
      <div
        className="row gap10"
        style={{
          flexWrap: 'wrap',
          rowGap: 6,
          padding: '9px 16px',
          borderBottom: '0.5px solid var(--border)',
          background: 'var(--surface)',
          flexShrink: 0,
        }}
      >
        <button className="btn btn-ghost sm" onClick={() => navigate(topicPath(ms.project_id, 'writer'))}>
          <Icon name="chevron" size={13} style={{ transform: 'rotate(180deg)' }} />
          {tr('稿件', 'Manuscripts')}
        </button>
        <div className="row gap8 row-nowrap" style={{ flex: '1 1 180px', minWidth: 0 }}>
          <span
            title={ms.title}
            style={{
              fontSize: 13,
              fontWeight: 600,
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
            title={tr('修改标题', 'Edit title')}
            disabled={titleMutation.isPending}
            onClick={() => setTitleEditOpen(true)}
          >
            <Icon name="pen" size={11} />
          </button>
          <StatusPill status={ms.status} sm />
          {/* 实时状态条出现时由它给进度链接，这里不再重复 */}
          {isWriting && ms.writing_voyage_id && !aiWriting && (
            <Link
              to={`/voyages/${ms.writing_voyage_id}`}
              style={{ color: 'var(--accent-text)', fontSize: 12, whiteSpace: 'nowrap', flexShrink: 0 }}
            >
              {tr('查看进度', 'View progress')}
            </Link>
          )}
        </div>

        {/* 保存连接状态点（单人使用，不再显示协作者头像） */}
        {currentFile && !currentFile.readonly && !currentFile.is_binary && (
          <span
            title={
              wsStatus === 'connected'
                ? tr('改动已实时保存', 'Changes are saved as you type')
                : wsStatus === 'connecting'
                  ? tr('正在连接…', 'Connecting…')
                  : tr('连接已断开', 'Disconnected')
            }
            style={{
              width: 7,
              height: 7,
              borderRadius: '50%',
              flexShrink: 0,
              background:
                wsStatus === 'connected' ? 'var(--ok)' : wsStatus === 'connecting' ? 'var(--warn)' : 'var(--danger)',
            }}
          />
        )}

        <button className="btn btn-ghost sm" onClick={() => setFactOpen(true)}>
          <Icon name="layers" size={13} />
          {tr('事实包', 'Fact pack')}
        </button>
        {/* 主文件 + 编译器（Overleaf 式；编译时后端按此读取） */}
        <select
          className="input"
          style={{ height: 28, fontSize: 12, maxWidth: 150, padding: '0 22px 0 8px' }}
          title={tr('编译主文件', 'Main file')}
          value={ms.main_tex}
          disabled={settingsMutation.isPending || texFiles.length === 0}
          onChange={(e) => settingsMutation.mutate({ main_tex: e.target.value })}
        >
          {texFiles.every((f) => f.path !== ms.main_tex) && (
            <option value={ms.main_tex}>{ms.main_tex}</option>
          )}
          {texFiles.map((f) => (
            <option key={f.id} value={f.path}>{f.path}</option>
          ))}
        </select>
        <select
          className="input"
          style={{ height: 28, fontSize: 12, maxWidth: 120, padding: '0 22px 0 8px' }}
          title={tr('编译器', 'Compiler')}
          value={ms.engine}
          disabled={settingsMutation.isPending}
          onChange={(e) => settingsMutation.mutate({ engine: e.target.value as CompileEngine })}
        >
          {ms.engine !== 'tectonic' &&
            ms.engine !== 'pdflatex' &&
            ms.engine !== 'xelatex' &&
            ms.engine !== 'lualatex' && <option value={ms.engine}>{ms.engine}</option>}
          <option value="tectonic">tectonic</option>
          <option value="pdflatex">pdfLaTeX</option>
          <option value="xelatex">XeLaTeX</option>
          <option value="lualatex">LuaLaTeX</option>
        </select>
        <button
          className="btn btn-primary sm"
          disabled={compileMutation.isPending}
          title={tr('也可按 ⌘S', 'Or press ⌘S')}
          onClick={() => compileMutation.mutate()}
        >
          {compileMutation.isPending ? (
            <>
              <Icon name="refresh" size={13} style={{ animation: 'spin 1s linear infinite' }} />
              {tr('编译中…', 'Compiling…')}
            </>
          ) : (
            <>
              <Icon name="play" size={13} />
              {tr('编译', 'Compile')}
            </>
          )}
        </button>
        <MoreMenu
          items={[
            {
              key: 'refs',
              icon: 'book',
              label: refreshRefsMutation.isPending ? tr('更新参考文献中…', 'Updating references…') : tr('更新参考文献', 'Update references'),
              title: tr(
                '用课题的相关文献和关联文献库重新生成 references.bib',
                'Rebuild references.bib from this topic’s related papers and linked libraries',
              ),
              disabled: refreshRefsMutation.isPending,
              onSelect: () => refreshRefsMutation.mutate(),
            },
            {
              key: 'arxiv',
              icon: 'download',
              label: exportArxivMutation.isPending ? tr('导出中…', 'Exporting…') : tr('导出 arXiv 包', 'Export for arXiv'),
              title: tr('下载可直接上传 arXiv 的 tar.gz', 'Download a tar.gz ready for arXiv'),
              disabled: exportArxivMutation.isPending,
              onSelect: () => exportArxivMutation.mutate(),
            },
            // AI 披露声明（#691）：投稿合规入口，从平台留痕一键生成
            {
              key: 'disclosure',
              icon: 'shield',
              label: tr('AI 使用声明', 'AI statement'),
              onSelect: () => setDisclosureOpen(true),
            },
            {
              key: 'submit',
              icon: 'arrow',
              label: submitMutation.isPending ? tr('提交中…', 'Submitting…') : tr('投稿', 'Submit'),
              title: ms.status === 'submitted' ? tr('已投稿', 'Submitted') : tr('提交投稿审批', 'Request submission approval'),
              disabled: submitMutation.isPending || isWriting || ms.status === 'submitted',
              onSelect: onSubmit,
            },
          ]}
        />
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
          {tr('与本机引擎的连接已断开，正在重连。改动不会丢失，重连后自动保存。', 'Lost connection to the local engine. Reconnecting… Your changes will be saved once it’s back.')}
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
            <span>{tr('正在编译…', 'Compiling…')}</span>
          ) : aiWriting.phase === 'done' ? (
            <span>{tr('AI 起草中…', 'AI is drafting…')}</span>
          ) : (
            <span>
              {(() => {
                const sec = aiWriting.section ? sectionText(aiWriting.section) : tr('正文', 'body');
                return aiWriting.phase === 'revising'
                  ? tr(`AI 正在修订「${sec}」…`, `AI is revising ${sec}…`)
                  : tr(`AI 正在撰写「${sec}」…`, `AI is writing ${sec}…`);
              })()}
            </span>
          )}
          {aiFile && aiWriting.fileId !== currentFileId && (
            <button
              className="btn btn-ghost sm"
              style={{ height: 22, fontSize: 11, padding: '0 8px', marginLeft: 4 }}
              onClick={() => setCurrentFileId(aiFile.id)}
            >
              {tr(`打开 ${aiFile.path}`, `Open ${aiFile.path}`)}
            </button>
          )}
          <span style={{ flex: 1 }} />
          {ms.writing_voyage_id && (
            <Link
              to={`/voyages/${ms.writing_voyage_id}`}
              style={{ color: 'var(--accent-text)', textDecoration: 'underline', fontSize: 11 }}
            >
              {tr('查看进度', 'View progress')}
            </Link>
          )}
        </div>
      )}

      {/* —— 三栏（可拖拽调宽 / 可收起） —— */}
      <div className="workbench-panx" style={{ flex: 1, minHeight: 0 }}>
        <div ref={splitRef} className="workbench-panx-inner" style={{ height: '100%', display: 'flex' }}>
          {/* 左：文件树 */}
          {layout.leftOpen && (
            <div
              style={{
                width: leftW,
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
                  onCreateFolder={(path) => createFolderMutation.mutate(path)}
                  onUpload={(file) => uploadFileMutation.mutate(file)}
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
            toggleTitle={layout.leftOpen ? tr('收起文件列表', 'Collapse file list') : tr('展开文件列表', 'Expand file list')}
            chevronDeg={layout.leftOpen ? 180 : 0}
          />

          {/* 中：编辑器 */}
          <div style={{ flex: 1, display: 'flex', flexDirection: 'column', minWidth: 0, minHeight: 0, background: 'var(--surface)' }}>
            {currentFile ? (
              currentFile.is_binary ? (
                <>
                  <div
                    className="row gap8"
                    style={{ padding: '6px 14px', borderBottom: '0.5px solid var(--border)', flexShrink: 0 }}
                  >
                    <span className="mono" style={{ fontSize: 11, color: 'var(--text-3)' }}>{currentFile.path}</span>
                    <span className="pill sm" style={{ height: 17, fontSize: 11 }}>{tr('二进制文件', 'Binary file')}</span>
                  </div>
                  <BinaryPreview manuscriptId={ms.id} file={currentFile} />
                </>
              ) : (
              <>
                <div
                  className="row gap8"
                  style={{ padding: '6px 14px', borderBottom: '0.5px solid var(--border)', flexShrink: 0 }}
                >
                  <span className="mono" style={{ fontSize: 11, color: 'var(--text-3)' }}>{currentFile.path}</span>
                  {currentFile.readonly && (
                    <span className="pill sm" style={{ height: 17, fontSize: 11 }}>{tr('只读', 'Read-only')}</span>
                  )}
                  <button
                    className="writer-mini-btn"
                    style={{ marginLeft: 'auto' }}
                    title={tr('查找 / 替换（⌘F）', 'Find / replace (⌘F)')}
                    disabled={!view}
                    onClick={() => view && openSearchPanel(view)}
                  >
                    <Icon name="search" size={11} />
                  </button>
                  <button
                    className="writer-mini-btn"
                    title={tr('版本历史', 'Version history')}
                    onClick={() => setHistoryOpen(true)}
                  >
                    <Icon name="clock" size={11} />
                  </button>
                  <span className="mono" style={{ fontSize: 11, color: 'var(--text-3)' }}>
                    {tr('⌘S 编译', '⌘S to compile')}
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
                {/* 编辑器下方：内联 AI（润色 / 改写 / 续写）操作条 */}
                {!currentFile.readonly && (
                  <div
                    className="row gap6"
                    style={{
                      flexWrap: 'wrap',
                      rowGap: 4,
                      padding: '6px 14px',
                      borderTop: '0.5px solid var(--border)',
                      background: 'var(--surface-2)',
                      flexShrink: 0,
                    }}
                  >
                    <span
                      className="mono"
                      title={writingModel ? tr(`写作所用模型：${writingModel}`, `Writing model: ${writingModel}`) : tr('写作所用模型', 'Writing model')}
                      style={{
                        fontSize: 11,
                        fontWeight: 600,
                        color: 'var(--text-3)',
                        marginRight: 4,
                        display: 'inline-flex',
                        alignItems: 'center',
                        gap: 4,
                        maxWidth: 200,
                        overflow: 'hidden',
                        textOverflow: 'ellipsis',
                        whiteSpace: 'nowrap',
                      }}
                    >
                      <Icon name="sparkle" size={11} style={{ flexShrink: 0, color: 'var(--accent)' }} />
                      {writingModel ?? (routesQuery.isLoading ? tr('加载中…', 'Loading…') : tr('AI 写作', 'AI writing'))}
                    </span>
                    <button
                      className="btn btn-ghost sm"
                      style={{ height: 24, fontSize: 11 }}
                      disabled={isWriting}
                      title={isWriting ? tr('AI 正在起草', 'AI is drafting') : tr('根据事实包逐节起草', 'Draft section by section from the fact pack')}
                      onClick={() => setDraftOpen(true)}
                    >
                      <Icon name="sparkle" size={11} />
                      {isWriting ? tr('起草中…', 'Drafting…') : tr('AI 起草', 'Draft with AI')}
                    </button>
                    {(['polish', 'rewrite', 'continue'] as const).map((m) => (
                      <button
                        key={m}
                        className={`btn sm ${assistMode === m ? 'btn-soft' : 'btn-ghost'}`}
                        style={{ height: 24, fontSize: 11 }}
                        disabled={!view || isWriting}
                        title={
                          m === 'continue'
                            ? tr('从光标处接着写', 'Keep writing from the cursor')
                            : m === 'polish'
                              ? tr('润色选中的文字', 'Polish the selected text')
                              : tr('按你的要求改写选中的文字', 'Rewrite the selected text your way')
                        }
                        onClick={() => setAssistMode((cur) => (cur === m ? null : m))}
                      >
                        <Icon name="sparkle" size={11} />
                        {m === 'polish' ? tr('润色', 'Polish') : m === 'rewrite' ? tr('改写', 'Rewrite') : tr('续写', 'Continue')}
                      </button>
                    ))}
                  </div>
                )}
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
              )
            ) : (
              <div style={{ flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
                <EmptyState compact icon="file" title={tr('还没有文件', 'No files yet')} desc={tr('点击左侧 + 新建 .tex 文件。', 'Click + on the left to add a .tex file.')} />
              </div>
            )}
          </div>

          <Gutter
            dragging={dragging === 'right'}
            onMouseDown={rightOpen ? onRightGutterDown : undefined}
            onReset={() => patchLayout({ rightW: DEFAULT_LAYOUT.rightW })}
            collapsed={!rightOpen}
            onToggle={toggleRight}
            toggleTitle={rightOpen ? tr('收起预览面板', 'Collapse preview panel') : tr('展开预览面板', 'Expand preview panel')}
            chevronDeg={rightOpen ? 0 : 180}
          />

          {/* 右：PDF 预览 + 诊断 */}
          {rightOpen && (
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
                    title={layout.pdfOpen ? tr('收起 PDF 预览', 'Collapse PDF preview') : tr('展开 PDF 预览', 'Expand PDF preview')}
                    onClick={() => patchLayout({ pdfOpen: !layout.pdfOpen })}
                  >
                    <Icon name="chevDown" size={11} style={{ transform: layout.pdfOpen ? 'none' : 'rotate(-90deg)', transition: 'transform .12s' }} />
                  </button>
                  <span style={{ fontSize: 12, fontWeight: 600, color: 'var(--text-2)' }}>{tr('PDF 预览', 'PDF preview')}</span>
                  {compile && (
                    <span className="mono" style={{ fontSize: 11, color: 'var(--text-3)', marginLeft: 'auto' }}>
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
                    title={layout.diagOpen ? tr('收起编译问题', 'Collapse compile issues') : tr('展开编译问题', 'Expand compile issues')}
                    onClick={() => patchLayout({ diagOpen: !layout.diagOpen })}
                  >
                    <Icon name="chevDown" size={11} style={{ transform: layout.diagOpen ? 'none' : 'rotate(-90deg)', transition: 'transform .12s' }} />
                  </button>
                  <span style={{ fontSize: 12, fontWeight: 600, color: 'var(--text-2)' }}>{tr('编译问题', 'Compile issues')}</span>
                  {compile && (
                    <span className="mono" style={{ fontSize: 11, marginLeft: 'auto' }}>
                      {errCount > 0 && <span style={{ color: 'var(--danger-tx)', fontWeight: 600 }}>{tr(`${errCount} 个错误`, `${errCount} ${errCount === 1 ? 'error' : 'errors'}`)}</span>}
                      {errCount > 0 && warnCount > 0 && <span style={{ color: 'var(--text-3)' }}> · </span>}
                      {warnCount > 0 && <span style={{ color: 'var(--warn-tx)', fontWeight: 600 }}>{tr(`${warnCount} 个警告`, `${warnCount} ${warnCount === 1 ? 'warning' : 'warnings'}`)}</span>}
                      {errCount === 0 && warnCount === 0 && <span style={{ color: 'var(--ok-tx)' }}>{tr('没有问题', 'No issues')}</span>}
                    </span>
                  )}
                </div>
                {layout.diagOpen && (
                  <div className="scroll" style={{ flex: 1, overflowY: 'auto', padding: '4px 0' }}>
                    {!compile ? (
                      <div className="empty" style={{ padding: 24, fontSize: 12 }}>{tr('编译后，错误和警告会显示在这里。', 'Errors and warnings appear here after you compile.')}</div>
                    ) : compile.diagnostics.length === 0 ? (
                      <div className="empty" style={{ padding: 24, fontSize: 12 }}>
                        {compile.status === 'ok' ? tr('没有错误或警告', 'No errors or warnings') : tr('编译没有返回详细信息', 'The compile returned no details')}
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
      </div>

      {/* —— 抽屉 / Modal —— */}
      <PromptModal
        open={titleEditOpen}
        onClose={() => setTitleEditOpen(false)}
        title={tr('修改标题', 'Edit title')}
        initial={ms.title}
        placeholder={tr('标题', 'Title')}
        submitText={tr('保存', 'Save')}
        busy={titleMutation.isPending}
        onSubmit={(title) => {
          if (title !== ms.title) titleMutation.mutate(title);
          setTitleEditOpen(false);
        }}
      />
      <ConfirmModal
        open={submitConfirmOpen}
        onClose={() => setSubmitConfirmOpen(false)}
        title={tr('提交投稿审批？', 'Request submission approval?')}
        message={tr('在审批中批准后，稿件会标记为「已投稿」。', 'Once you approve it, the manuscript is marked as submitted.')}
        confirmText={tr('提交', 'Submit')}
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
      <DraftModal open={draftOpen} onClose={() => setDraftOpen(false)} manuscript={ms} onInitialized={handleInitialized} />
      <AiDisclosureModal
        open={disclosureOpen}
        onClose={() => setDisclosureOpen(false)}
        subject={{ kind: 'manuscript', id: ms.id }}
      />
    </div>
  );
}
