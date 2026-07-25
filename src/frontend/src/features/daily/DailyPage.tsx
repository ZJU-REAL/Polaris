import { Fragment, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';
import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Icon } from '../../components/ui/Icon';
import { PageHead } from '../../components/ui/PageHead';
import { EmptyState } from '../../components/ui/EmptyState';
import {
  FigureEmbed,
  FiguresSection,
  hasEmbeddedFigures,
  usePaperFigures,
} from '../../components/ui/FigureGallery';
import { CompileBadge } from '../../components/ui/CompileBadge';
import { Segmented } from '../../components/ui/Segmented';
import { toast } from '../../components/ui/Toast';
import {
  ApiError,
  api,
  type DailyPaperItem,
  type DailySort,
  type PaperDetail,
  type ReadingStatus,
} from '../../lib/api';
import { fmtTime } from '../../lib/format';
import { tr } from '../../lib/i18n';
import { Markdown } from '../../lib/markdown';
import { usePendingByPaper } from '../../lib/pending';
import { PaperReader } from '../wiki/PaperReader';
import { readerFrom } from '../reading/shared';
import {
  AdvancedPanel,
  AdvancedToggle,
  AffiliationChips,
  AuthorLinks,
  FilterInput,
  MetaItem,
  SearchInput,
  SemanticSwitch,
  saveBlob,
  useDebounced,
} from '../wiki/shared';
import {
  ConceptChips,
  PaperMyMetaRow,
  PaperMyTagsRow,
  PaperNotesSection,
  PaperTagsRow,
  WikiHeaderActions,
} from '../shared/PaperDetailBlocks';
import { DailyLikes } from './DailyLikes';
import { DailyChatTab } from './DailyChatTab';
import { CollectTreeModal, type CollectPaperRef } from './CollectTreeModal';
import { PaperProgressModal } from '../library/PaperProgressModal';

/* ============================================================
   /daily — 每日新论文：arxiv 每日新提交（订阅分类内），保留最近 7 天。
   双栏主从布局对齐共享库浏览（LibraryBrowse）：
   左栏按日期分组的列表（排序 / 搜索 / 分页 / 行内点赞），右栏选中论文详情。
   ============================================================ */

const PAGE_SIZE = 20;

const EN_MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

/** 'YYYY-MM-DD' → 「7月24日 · 32 篇」/ "Jul 24 · 32 papers"（count 未知时只显示日期）。 */
function dayLabel(iso: string, count: number | undefined): string {
  const parts = iso.split('-');
  const m = Number(parts[1] ?? 0);
  const d = Number(parts[2] ?? 0);
  const en = `${EN_MONTHS[m - 1] ?? iso} ${d}`;
  if (count === undefined) return tr(`${m}月${d}日`, en);
  return tr(`${m}月${d}日 · ${count} 篇`, `${en} · ${count} papers`);
}

/** 作者行：前 3 名 + et al。 */
function authorsBrief(p: DailyPaperItem): string {
  const names = p.authors.map((a) => a.name).filter(Boolean);
  if (names.length === 0) return '';
  return names.length > 3 ? `${names.slice(0, 3).join(', ')} et al.` : names.join(', ');
}

/** 类型徽标：new=绿色 NEW；cross=「更新」（保持原角标样式，仅换文案）。 */
function AnnounceBadge({ type }: { type: DailyPaperItem['announce_type'] }) {
  if (type === 'new') {
    return (
      <span
        className="pill sm"
        style={{ background: 'var(--ok-bg)', color: 'var(--ok-tx)', fontWeight: 700, letterSpacing: '0.05em', flexShrink: 0 }}
      >
        NEW
      </span>
    );
  }
  return (
    <span className="pill sm" style={{ background: 'var(--warn-bg)', color: 'var(--warn-tx)', flexShrink: 0 }}>
      {tr('更新', 'Updated')}
    </span>
  );
}

function DailyRow({
  p,
  active,
  checked,
  selectMode,
  onClick,
  onToggleCheck,
}: {
  p: DailyPaperItem;
  active: boolean;
  checked: boolean;
  selectMode: boolean;
  onClick: () => void;
  onToggleCheck: () => void;
}) {
  return (
    <div
      onClick={onClick}
      style={{
        padding: '11px 14px 11px 16px',
        cursor: 'pointer',
        borderBottom: '0.5px solid var(--border)',
        background: active ? 'var(--accent-soft)' : 'transparent',
        borderLeft: active ? '2px solid var(--accent)' : '2px solid transparent',
        transition: 'background 0.12s',
      }}
    >
      <div style={{ display: 'flex', gap: 8, alignItems: 'flex-start' }}>
        {/* 占位常驻：切换多选时行内容不左右跳 */}
        <input
          type="checkbox"
          checked={checked}
          onClick={(e) => e.stopPropagation()}
          onChange={onToggleCheck}
          title={tr('选中后可批量导出引用', 'Select for bulk citation export')}
          style={{
            width: 13,
            height: 13,
            margin: '2px 0 0',
            flexShrink: 0,
            accentColor: 'var(--accent)',
            cursor: 'pointer',
            visibility: selectMode ? 'visible' : 'hidden',
          }}
        />
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ fontSize: 13, fontWeight: 600, lineHeight: 1.35 }}>{p.title}</div>
          <div className="row gap8" style={{ marginTop: 5, fontSize: 11, color: 'var(--text-3)' }}>
            <span className="pill sm mono" style={{ background: 'var(--surface-3)', flexShrink: 0 }}>
              {p.primary_category}
            </span>
            <AnnounceBadge type={p.announce_type} />
            <span
              style={{ flex: 1, minWidth: 0, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}
              title={p.authors.map((a) => a.name).join(', ')}
            >
              {authorsBrief(p)}
            </span>
            {p.has_wiki && (
              <span
                title={tr('已有 AI 解读', 'AI summary available')}
                style={{ display: 'inline-flex', color: 'var(--accent)', flexShrink: 0 }}
              >
                <Icon name="file" size={11} />
              </span>
            )}
          </div>
          {/* 第三行：[♥] 点赞者头像（单行，超出用 +N） …… N 人赞过 */}
          <DailyLikes item={p} variant="row" />
        </div>
      </div>
    </div>
  );
}

function DailyDetailPane({
  entryId,
  onCollect,
  downloading,
  compiling,
  onFetchPdf,
  onCompile,
  onFilterAuthor,
  onFilterAffiliation,
}: {
  entryId: string;
  onCollect: (p: CollectPaperRef) => void;
  /** 这篇正在下载原文（状态存在父组件，切走再切回来照样是「下载中」） */
  downloading: boolean;
  /** 这篇正在 AI 编译 */
  compiling: boolean;
  onFetchPdf: (entryId: string) => void;
  onCompile: (entryId: string) => void;
  /** 点作者 → 按该作者过滤列表 */
  onFilterAuthor: (name: string) => void;
  /** 点机构 chip → 按该机构过滤列表 */
  onFilterAffiliation: (name: string) => void;
}) {
  const navigate = useNavigate();
  const location = useLocation();
  const [readerOpen, setReaderOpen] = useState(false);
  // 阅览模式打开后是否直接唤起打印（「导出 PDF」一步直达）
  const [readerPrint, setReaderPrint] = useState(false);
  const openReader = useCallback(() => {
    setReaderPrint(false);
    setReaderOpen(true);
  }, []);
  const openReaderForPrint = useCallback(() => {
    setReaderPrint(true);
    setReaderOpen(true);
  }, []);
  const { data: paper, isLoading, isError } = useQuery({
    queryKey: ['daily-paper', entryId],
    queryFn: () => api.getDailyPaper(entryId),
    retry: false,
  });

  /* 内容池里这篇的常规详情：星标 / 阅读状态 / 标签 / 笔记数 / TL;DR / doi 都在这上面
     （每日详情是榜单视角，没有这些个人维度字段）。queryKey 与文献库、相关研究、
     我的文献库同一个 ['paper', id]，缓存互通。 */
  const poolId = paper?.paper_id ?? null;
  const poolPaperQuery = useQuery({
    queryKey: ['paper', poolId],
    queryFn: () => api.getPaper(poolId ?? ''),
    enabled: !!poolId,
    retry: false,
  });
  const poolPaper = poolPaperQuery.data;
  const poolKey = useMemo(() => ['paper', poolId], [poolId]);
  const poolKeys = useMemo(() => [poolKey], [poolKey]);

  /* 正文 ![[fig:N]] 嵌入图（与文献库详情同款渲染）。
     每日详情返回的是 DailyPaperDetail（没有 figures 字段），图片按内容池 paper_id 单独拉一次。 */
  const figureRef = useMemo(() => (paper ? { id: paper.paper_id } : undefined), [paper]);
  const figures = usePaperFigures(figureRef);
  const renderFigure = useCallback(
    (n: number) => {
      const fig = figures.find((f) => f.index === n);
      return fig && paper ? <FigureEmbed paperId={paper.paper_id} fig={fig} /> : null;
    },
    [figures, paper],
  );

  if (isLoading) return <div className="empty">{tr('加载论文详情…', 'Loading paper…')}</div>;
  if (isError || !paper) {
    return (
      <EmptyState
        compact
        icon="x"
        title={tr('无法加载论文详情', 'Failed to load paper')}
        desc={tr('后端不可用或该论文已过期。', 'Backend unavailable or the paper has expired.')}
      />
    );
  }

  // 「链接放 arxiv，不放标题」：优先原文 url，退回 arxiv abs 页
  const arxivHref = paper.url ?? (paper.arxiv_id ? `https://arxiv.org/abs/${paper.arxiv_id}` : null);
  // PDF 不可用时的下载去处：arxiv pdf，无 arxiv_id 退回原文 url
  const pdfDownloadUrl = paper.arxiv_id ? `https://arxiv.org/pdf/${paper.arxiv_id}` : paper.url;
  // 复用常规详情的阅览器：把每日详情映射成 PaperReader 需要的字段（id 用内容池 paper_id）
  const readerPaper: PaperDetail = {
    ...(paper as unknown as PaperDetail),
    id: paper.paper_id,
    venue: null,
    tldr: poolPaper?.tldr ?? null,
    concepts: paper.concepts ?? [],
    figures,
  };
  const starred = poolPaper?.starred ?? false;
  const readingStatus: ReadingStatus = poolPaper?.reading_status ?? 'unread';

  return (
    <div className="scroll fadeup" key={paper.entry_id} style={{ overflowY: 'auto', flex: 1, padding: '26px 32px 60px' }}>
      <div className="row gap8 wrap" style={{ marginBottom: 8 }}>
        {[paper.primary_category, ...paper.categories.filter((c) => c !== paper.primary_category)].map((c) => (
          <span key={c} className="pill sm mono" style={{ background: 'var(--surface-3)' }}>
            {c}
          </span>
        ))}
        <AnnounceBadge type={paper.announce_type} />
        {paper.arxiv_id &&
          (arxivHref ? (
            <a
              className="pill sm mono"
              href={arxivHref}
              target="_blank"
              rel="noreferrer noopener"
              style={{ background: 'var(--surface-3)', textDecoration: 'none', color: 'var(--accent)' }}
              title={tr('打开 arXiv 页面', 'Open on arXiv')}
            >
              arXiv:{paper.arxiv_id}
            </a>
          ) : (
            <span className="pill sm mono" style={{ background: 'var(--surface-3)' }}>
              arXiv:{paper.arxiv_id}
            </span>
          ))}
      </div>

      {/* 标题永远是纯文本（不可点）；链接放上面的 arXiv chip */}
      <h1 style={{ fontSize: 21, fontWeight: 680, lineHeight: 1.3, margin: '2px 0 6px', letterSpacing: '-0.01em' }}>
        {paper.title}
      </h1>
      {/* 作者 / 机构都可点：点了按它过滤列表 */}
      <AuthorLinks authors={paper.authors} onFilter={onFilterAuthor} />
      <AffiliationChips affiliations={paper.affiliations} onFilter={onFilterAffiliation} />
      {paper.published_at && (
        <div style={{ fontSize: 11.5, color: 'var(--text-4)', margin: '6px 0 14px' }}>
          {tr('发布于', 'Published')} {fmtTime(paper.published_at)}
        </div>
      )}

      {/* —— 操作栏（对齐常规论文详情 PaperDetailPane） —— */}
      <div className="row gap8 wrap">
        {paper.pdf_available ? (
          <button
            className="btn btn-primary sm"
            onClick={() =>
              navigate(`/papers/${paper.paper_id}/read`, { state: readerFrom(location, 'daily') })
            }
          >
            <Icon name="file" size={13} />
            {tr('阅读原文', 'Read original')}
          </button>
        ) : (
          (paper.arxiv_id || pdfDownloadUrl) && (
            <button
              className="btn btn-primary sm"
              disabled={downloading}
              onClick={() => onFetchPdf(paper.entry_id)}
              title={tr('下载 PDF 到平台，下载后即可在线阅读', 'Fetch the PDF into Polaris so it can be read here')}
            >
              {downloading ? (
                <>
                  <Icon name="refresh" size={13} style={{ animation: 'spin 1s linear infinite' }} />
                  {tr('下载中…', 'Downloading…')}
                </>
              ) : (
                <>
                  <Icon name="download" size={13} />
                  {tr('下载原文', 'Download PDF')}
                </>
              )}
            </button>
          )
        )}
        <button
          className="btn btn-soft sm"
          title={
            paper.has_wiki
              ? tr('用最新的图文模式重写这篇介绍', 'Rewrite this intro with the latest text+figures mode')
              : tr('AI 精读并编译图文介绍', 'Have the AI read and compile an illustrated intro')
          }
          disabled={compiling}
          onClick={() => onCompile(paper.entry_id)}
        >
          {compiling ? (
            <>
              <Icon name="refresh" size={13} style={{ animation: 'spin 1s linear infinite' }} />
              {tr('AI 编译中，约半分钟…', 'Compiling — about half a minute…')}
            </>
          ) : (
            <>
              <Icon name="sparkle" size={13} />
              {paper.has_wiki ? tr('重新编译', 'Recompile') : tr('编译', 'Compile')}
            </>
          )}
        </button>
        {paper.wiki_content && (
          <button
            className="btn btn-soft sm"
            title={tr('全屏阅览图文介绍，可导出 PDF', 'Full-screen reading view, exportable to PDF')}
            onClick={openReader}
          >
            <Icon name="book" size={13} />
            {tr('阅览模式', 'Reading mode')}
          </button>
        )}
        <button
          className="btn btn-primary sm"
          onClick={() => onCollect({ paper_id: paper.paper_id, entry_id: paper.entry_id, title: paper.title })}
        >
          <Icon name="plus" size={13} />
          {tr('收进文献库', 'Add to libraries')}
        </button>
        <DailyLikes item={paper} />
      </div>

      {/* —— 个人状态：星标 + 阅读状态（内容池维度，和文献库里是同一条记录） —— */}
      {poolPaper && (
        <PaperMyMetaRow
          paperId={poolPaper.id}
          starred={starred}
          readingStatus={readingStatus}
          detailKey={poolKey}
        />
      )}

      {/* —— 标签：库标签只读（编辑在文献工作台）+ 我的标签就地改 —— */}
      <PaperTagsRow tags={poolPaper?.tags} />
      {poolPaper && (
        <PaperMyTagsRow paperId={poolPaper.id} myTags={poolPaper.my_tags} detailKey={poolKey} />
      )}

      {/* —— frontmatter 风格元数据卡 —— */}
      <div className="card card-pad" style={{ margin: '18px 0 0', background: 'var(--surface-2)', padding: '14px 18px' }}>
        <MetaItem label="arxiv_id">
          {paper.arxiv_id ? <span className="mono">{paper.arxiv_id}</span> : <span className="muted">—</span>}
        </MetaItem>
        <MetaItem label="doi">
          {poolPaper?.doi ? <span className="mono">{poolPaper.doi}</span> : <span className="muted">—</span>}
        </MetaItem>
        <MetaItem label="published">
          {paper.published_at ? (
            <span className="mono">{paper.published_at.slice(0, 10)}</span>
          ) : (
            <span className="muted">—</span>
          )}
        </MetaItem>
        <MetaItem label={tr('编译时间', 'compiled at')}>
          {paper.compiled_at ? (
            <span className="mono">{fmtTime(paper.compiled_at)}</span>
          ) : (
            <span className="muted">{tr('未编译', 'not compiled')}</span>
          )}
        </MetaItem>
      </div>

      {paper.abstract ? (
        <div className="card card-pad" style={{ background: 'var(--surface-2)', marginTop: 18 }}>
          <div className="row gap8" style={{ marginBottom: 8 }}>
            <Icon name="file" size={14} style={{ color: 'var(--accent)' }} />
            <span style={{ fontSize: 12, fontWeight: 700 }}>{tr('摘要', 'Abstract')}</span>
          </div>
          <p style={{ fontSize: 13.5, lineHeight: 1.7, margin: 0 }}>{paper.abstract}</p>
        </div>
      ) : (
        <div className="empty" style={{ padding: 20, marginTop: 18 }}>
          {tr('这篇还没有摘要。', 'No abstract for this paper.')}
        </div>
      )}

      {/* —— TL;DR（来自内容池详情） —— */}
      {poolPaper?.tldr && (
        <div
          style={{
            marginTop: 18,
            padding: '12px 16px',
            borderRadius: 10,
            background: 'var(--accent-soft)',
            fontSize: 13,
            lineHeight: 1.65,
            color: 'var(--text)',
          }}
        >
          <span className="mono" style={{ fontSize: 10.5, color: 'var(--accent-text)', display: 'block', marginBottom: 4 }}>
            TL;DR
          </span>
          {poolPaper.tldr}
        </div>
      )}

      {/* —— 概念 chips（与库版同款；每日没有概念库 tab，故只展示不跳转） —— */}
      <ConceptChips concepts={paper.concepts} />

      {/* —— 我的笔记（只有自己看得到） —— */}
      {poolPaper && (
        <PaperNotesSection
          paperId={poolPaper.id}
          noteCount={poolPaper.note_count ?? 0}
          invalidateKeys={poolKeys}
        />
      )}

      {/* —— 重要图片画廊（只读：提取/重新提取是库维护动作，普通用户没权限） —— */}
      <FiguresSection
        paper={readerPaper}
        readOnly
        defaultCollapsed={hasEmbeddedFigures(paper.wiki_content, figures)}
      />

      {/* —— AI 图文介绍：渲染风格对齐文献库详情（同容器/字号/Markdown props） —— */}
      {paper.wiki_content ? (
        <div style={{ marginTop: 22 }}>
          <div
            className="row"
            style={{
              justifyContent: 'space-between',
              alignItems: 'center',
              paddingBottom: 10,
              marginBottom: 16,
              borderBottom: '0.5px solid var(--border)',
            }}
          >
            <div className="row gap8" style={{ minWidth: 0 }}>
              <span className="mono" style={{ fontSize: 11, color: 'var(--text-4)', letterSpacing: '0.04em' }}>
                {tr('AI 图文介绍', 'AI intro')}
              </span>
              <CompileBadge model={paper.wiki_model ?? null} at={paper.compiled_at ?? null} />
            </div>
            <WikiHeaderActions onRead={openReader} onExport={openReaderForPrint} />
          </div>
          <Markdown source={paper.wiki_content} renderFigure={renderFigure} />
        </div>
      ) : (
        <EmptyState
          compact
          icon="pen"
          title={tr('还没有 AI 介绍', 'No AI intro yet')}
          desc={tr(
            '点上方的编译按钮，让 AI 精读这篇论文并生成图文介绍。',
            'Hit the compile button above to have the AI read this paper and write an illustrated intro.',
          )}
        />
      )}

      {readerOpen && (
        <PaperReader
          paper={readerPaper}
          wikiContent={paper.wiki_content}
          renderFigure={renderFigure}
          autoPrint={readerPrint}
          onClose={() => setReaderOpen(false)}
        />
      )}
    </div>
  );
}

type DailyView = 'papers' | 'chat';
type AnnounceFilter = 'all' | 'new' | 'cross';

// 类型筛选默认值：只看新工作（高级检索面板里的「恢复默认」也回到这个值）
const DEFAULT_ANNOUNCE: AnnounceFilter = 'new';

// 列表固定按点赞排序（没有排序切换 UI）；语义检索时后端按相关度排，忽略这个值
const DAILY_SORT: DailySort = 'likes';

export function DailyPage() {
  const queryClient = useQueryClient();
  const [view, setView] = useState<DailyView>('papers');
  const [qInput, setQInput] = useState('');
  const q = useDebounced(qInput.trim());
  // 语义检索开关（true=按意思检索，false=关键词字面匹配）
  const [semanticOn, setSemanticOn] = useState(false);
  const [page, setPage] = useState(1);
  // —— 日期（null=全部 7 天，默认落在最新一天）留在工具栏；分类 / 类型收进高级检索面板 ——
  const [day, setDay] = useState<string | null>(null);
  // 高级检索默认展开：分类 / 类型是常用筛选，藏起来用户找不到
  const [advOpen, setAdvOpen] = useState(true);
  const [category, setCategory] = useState('');
  const [announce, setAnnounce] = useState<AnnounceFilter>(DEFAULT_ANNOUNCE);
  // 作者 / 机构：手填，或在右栏详情里点作者名、点机构 chip 带进来
  const [authorInput, setAuthorInput] = useState('');
  const [affiliationInput, setAffiliationInput] = useState('');
  const author = useDebounced(authorInput.trim());
  const affiliation = useDebounced(affiliationInput.trim());
  // 高级条件是否偏离默认（决定高级检索按钮上的小圆点）
  const advActive = !!category || announce !== DEFAULT_ANNOUNCE || !!author || !!affiliation;
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [collectPaper, setCollectPaper] = useState<CollectPaperRef | null>(null);
  const [collectOpen, setCollectOpen] = useState(false);
  // 收录到库/课题/个人后若启动了后台补全，弹出与手动添加同款分阶段进度框
  const [progress, setProgress] = useState<{ taskId: string; title: string } | null>(null);
  // 多选（批量导出引用）：默认关闭，底部「多选」按钮开启后行首出现复选框；存 paper_id
  const [selectMode, setSelectMode] = useState(false);
  const [selected, setSelected] = useState<Set<string>>(new Set());

  useEffect(() => setPage(1), [q, semanticOn, day, category, announce, author, affiliation]);
  useEffect(() => {
    setSelected(new Set());
    setSelectMode(false);
  }, [q, semanticOn, day, category, announce, author, affiliation]);

  const daysQuery = useQuery({
    queryKey: ['daily-days'],
    queryFn: () => api.listDailyDays(),
    retry: false,
    staleTime: 60_000,
  });
  const dayCount = new Map((daysQuery.data ?? []).map((d) => [d.date, d.count] as const));

  // 有数据的日期，升序（旧 → 新），日期步进只在这些日期间跳
  const dates = (daysQuery.data ?? []).map((d) => d.date).sort();
  const latestDate = dates[dates.length - 1] ?? null;

  // 日期一改就算用户表过态，之后不再自动初始化
  const didInitDay = useRef(false);
  const pickDay = useCallback((d: string | null) => {
    didInitDay.current = true;
    setDay(d);
  }, []);
  // 默认只看当天：日期列表到手后落到最新一天（只做一次，不覆盖用户选择）
  useEffect(() => {
    if (didInitDay.current || !latestDate) return;
    didInitDay.current = true;
    setDay(latestDate);
  }, [latestDate]);

  const dayIdx = day ? dates.indexOf(day) : -1;
  // 「全部」视为最新一天的后一位：← 从全部进入最新一天；→ 在最新一天回到全部
  const canPrevDay = dates.length > 0 && (day === null || dayIdx > 0);
  const canNextDay = day !== null && dayIdx >= 0;
  const goPrevDay = () => {
    if (day === null) pickDay(latestDate);
    else if (dayIdx > 0) pickDay(dates[dayIdx - 1] ?? null);
  };
  const goNextDay = () => {
    if (day === null) return;
    pickDay(dayIdx >= 0 && dayIdx < dates.length - 1 ? (dates[dayIdx + 1] ?? null) : null);
  };

  // 点作者/机构 → 列表只留匹配的论文（走已有的高级检索），其余条件重置并展开面板；
  // 日期放开到全部 7 天——只看当天的话，按作者/机构筛基本什么都剩不下
  const applyAdvFilter = useCallback(
    (patch: { author?: string; affiliation?: string }) => {
      setSemanticOn(false);
      setQInput('');
      setAuthorInput(patch.author ?? '');
      setAffiliationInput(patch.affiliation ?? '');
      setCategory('');
      setAnnounce('all');
      setAdvOpen(true);
      pickDay(null);
      if (patch.author) {
        toast(tr(`已筛选作者：${patch.author}`, `Filtered by author: ${patch.author}`), 'info');
      } else if (patch.affiliation) {
        toast(tr(`已筛选机构：${patch.affiliation}`, `Filtered by affiliation: ${patch.affiliation}`), 'info');
      }
    },
    [pickDay],
  );
  const filterByAuthor = useCallback((name: string) => applyAdvFilter({ author: name }), [applyAdvFilter]);
  const filterByAffiliation = useCallback(
    (name: string) => applyAdvFilter({ affiliation: name }),
    [applyAdvFilter],
  );

  const categoriesQuery = useQuery({
    queryKey: ['daily-categories'],
    queryFn: () => api.getDailyCategories(),
    retry: false,
    staleTime: 300_000,
  });

  // 语义检索：只在有关键词时生效；结果按相关度排序、不分页
  const semantic = !!q && semanticOn;
  const listQuery = useQuery({
    queryKey: ['daily-papers', semanticOn, page, q, day, category, announce, author, affiliation],
    queryFn: () =>
      api.listDailyPapers({
        sort: semantic ? undefined : DAILY_SORT,
        page: semantic ? undefined : page,
        size: PAGE_SIZE,
        q: q || undefined,
        date: day ?? undefined,
        category: category || undefined,
        announce: announce === 'all' ? undefined : announce,
        author: author || undefined,
        affiliation: affiliation || undefined,
        mode: semantic ? 'semantic' : undefined,
      }),
    retry: false,
    placeholderData: keepPreviousData,
  });
  // 默认口径（当天 + 新工作）之外还加了条件才算「筛过」——空列表时给的话术不一样
  const filtered = !!q || advActive || (day !== null && day !== latestDate);
  const items = listQuery.data?.items ?? [];
  const total = listQuery.data?.total ?? 0;
  const pages = semantic ? 1 : Math.max(1, Math.ceil(total / PAGE_SIZE));
  // 后端明确回了「用的是关键词」→ 说明语义检索这会儿不可用
  const fallbackNotice = semantic && listQuery.data?.mode_used === 'keyword';

  // 首条自动选中
  const firstId = items[0]?.entry_id ?? null;
  useEffect(() => {
    if (!selectedId && firstId) setSelectedId(firstId);
  }, [selectedId, firstId]);

  const toggleSelected = (paperId: string) =>
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(paperId)) next.delete(paperId);
      else next.add(paperId);
      return next;
    });

  const exportMutation = useMutation({
    mutationFn: () => api.downloadDailyCitations({ format: 'bibtex', ids: [...selected] }),
    onSuccess: (blob) => {
      saveBlob(blob, 'polaris-daily-citations.bib');
      toast(tr(`已导出 ${selected.size} 篇的 BibTeX`, `Exported BibTeX for ${selected.size} papers`), 'ok');
    },
    onError: (e) =>
      toast(`${tr('导出失败：', 'Export failed: ')}${e instanceof Error ? e.message : String(e)}`, 'error'),
  });

  const openCollect = (p: CollectPaperRef) => {
    setCollectPaper(p);
    setCollectOpen(true);
  };

  // —— 两个长任务（下载原文 / AI 编译）都同步等着，用户很可能切走再切回来 ——
  // 进行中状态按 entry_id 记在这一层：详情面板换论文不会丢，多篇同时跑也各记各的。
  const downloadPending = usePendingByPaper();
  const compilePending = usePendingByPaper();

  const fetchPdf = useCallback(
    (entryId: string) => {
      void downloadPending.run(entryId, async () => {
        try {
          await api.fetchDailyPaperPdf(entryId);
          toast(tr('已下载原文，可以在线阅读了', 'PDF fetched — you can read it here now'), 'ok');
          void queryClient.invalidateQueries({ queryKey: ['daily-paper', entryId] });
        } catch (e) {
          toast(
            `${tr('下载原文失败', 'Failed to fetch the PDF')}：${e instanceof Error ? e.message : String(e)}`,
            'error',
          );
        }
      });
    },
    [downloadPending, queryClient],
  );

  // 单篇 AI 解读编译：同步等待（约半分钟）；409 = 已有人在编译
  const compile = useCallback(
    (entryId: string) => {
      void compilePending.run(entryId, async () => {
        try {
          await api.compileDailyPaper(entryId);
          void queryClient.invalidateQueries({ queryKey: ['daily-paper', entryId] });
          void queryClient.invalidateQueries({ queryKey: ['daily-papers'] }); // 列表行的 has_wiki 标记
          void queryClient.invalidateQueries({ queryKey: ['daily-liked'] });
          // 编译会写回内容池论文（TL;DR / 概念 / 机构），详情面板右侧那份也要刷
          void queryClient.invalidateQueries({ queryKey: ['paper'] });
        } catch (e) {
          if (e instanceof ApiError && e.status === 409) {
            toast(tr('已有人在生成，稍后刷新即可', 'Someone is already generating it, refresh later'), 'info');
            void queryClient.invalidateQueries({ queryKey: ['daily-paper', entryId] });
          } else {
            toast(
              `${tr('生成解读失败', 'Failed to generate summary')}：${e instanceof Error ? e.message : String(e)}`,
              'error',
            );
          }
        }
      });
    },
    [compilePending, queryClient],
  );

  return (
    <div
      className="page fadeup page-fill"
      style={{ maxWidth: 1360, paddingBottom: 24 }}
    >
      <PageHead
        eyebrow="Polaris · Daily Papers"
        title={tr('每日新论文', 'Daily Papers')}
        right={
          (categoriesQuery.data?.categories.length ?? 0) > 0 ? (
            <div className="row gap6 wrap" style={{ justifyContent: 'flex-end', maxWidth: 420 }}>
              <span style={{ fontSize: 11, color: 'var(--text-4)' }}>{tr('订阅分类', 'Subscribed')}</span>
              {categoriesQuery.data?.categories.map((c) => (
                <span key={c} className="pill sm mono" style={{ background: 'var(--surface-3)' }}>
                  {c}
                </span>
              ))}
            </div>
          ) : undefined
        }
      />

      <div className="row" style={{ marginBottom: 14 }}>
        <Segmented<DailyView>
          options={[
            { v: 'papers', label: tr('论文', 'Papers') },
            { v: 'chat', label: tr('对话', 'Chat') },
          ]}
          value={view}
          onChange={setView}
        />
      </div>

      <div
        className="card split-card"
      >
        {view === 'chat' ? (
          /* ======== 池对话：就最近 7 天的每日新论文问答 ======== */
          <DailyChatTab />
        ) : (
        <div className="split">
          {/* —— 左：按日期分组的列表 —— */}
          <div className="split-list">
            <div style={{ padding: '12px 14px 10px', borderBottom: '0.5px solid var(--border)' }}>
              <div className="row gap8">
                <SearchInput
                  value={qInput}
                  onChange={setQInput}
                  placeholder={
                    semanticOn
                      ? tr('语义检索（自然语言描述）…', 'Semantic search (natural language)…')
                      : tr('搜标题 / 摘要 / 作者…', 'Search title / abstract / authors…')
                  }
                />
                <SemanticSwitch checked={semanticOn} onChange={setSemanticOn} />
                <AdvancedToggle
                  open={advOpen}
                  active={advActive}
                  onToggle={() => setAdvOpen((o) => !o)}
                  title={tr(
                    '高级检索：分类 / 类型 / 作者 / 机构',
                    'Advanced search: category / type / author / affiliation',
                  )}
                />
                <span className="mono" style={{ fontSize: 10.5, color: 'var(--text-3)', flexShrink: 0 }}>
                  {total ? tr(`${total} 篇`, `${total}`) : ''}
                </span>
              </div>

              {/* 高级检索面板：分类 + 类型 + 作者 / 机构（日期步进和排序留在工具栏，它们是主导航） */}
              {advOpen && (
                <AdvancedPanel
                  onClear={
                    advActive
                      ? () => {
                          setCategory('');
                          setAnnounce(DEFAULT_ANNOUNCE);
                          setAuthorInput('');
                          setAffiliationInput('');
                        }
                      : undefined
                  }
                >
                  <div className="row gap6" style={{ alignItems: 'center' }}>
                    <span style={{ width: 34, flexShrink: 0, fontSize: 11, color: 'var(--text-3)' }}>
                      {tr('分类', 'Category')}
                    </span>
                    <select
                      className="input mono"
                      style={{ flex: 1, minWidth: 0, height: 26, fontSize: 11, padding: '0 6px' }}
                      value={category}
                      onChange={(e) => setCategory(e.target.value)}
                      title={tr('只看某个订阅分类的论文', 'Only papers in one subscribed category')}
                    >
                      <option value="">{tr('全部分类', 'All categories')}</option>
                      {(categoriesQuery.data?.categories ?? []).map((c) => (
                        <option key={c} value={c}>
                          {c}
                        </option>
                      ))}
                    </select>
                  </div>
                  <div className="row gap6 wrap" style={{ alignItems: 'center' }}>
                    <span style={{ width: 34, flexShrink: 0, fontSize: 11, color: 'var(--text-3)' }}>
                      {tr('类型', 'Type')}
                    </span>
                    <span className={`chip${announce === 'all' ? ' on' : ''}`} onClick={() => setAnnounce('all')}>
                      {tr('全部', 'All')}
                    </span>
                    <span className={`chip${announce === 'new' ? ' on' : ''}`} onClick={() => setAnnounce('new')}>
                      {tr('新工作', 'New')}
                    </span>
                    <span className={`chip${announce === 'cross' ? ' on' : ''}`} onClick={() => setAnnounce('cross')}>
                      {tr('更新', 'Updated')}
                    </span>
                  </div>
                  <div className="row gap8">
                    <FilterInput
                      value={authorInput}
                      onChange={setAuthorInput}
                      placeholder={tr('作者姓名…', 'Author name…')}
                    />
                    <FilterInput
                      value={affiliationInput}
                      onChange={setAffiliationInput}
                      placeholder={tr('发表机构…', 'Affiliation…')}
                      title={tr(
                        '机构信息要等这篇编译出解读后才有，没编译过的论文匹配不到',
                        'Affiliations only exist after a paper has been compiled here',
                      )}
                    />
                  </div>
                </AdvancedPanel>
              )}
              {/* 面板收起时，把正在生效的分类/类型如实说一句（默认「只看新工作」也算），
                  免得筛选藏进面板后用户不知道列表被过滤过 */}
              {!advOpen && (announce !== 'all' || !!category || !!author || !!affiliation) && (
                <div
                  onClick={() => setAdvOpen(true)}
                  style={{ marginTop: 6, fontSize: 11, color: 'var(--text-4)', cursor: 'pointer', lineHeight: 1.5 }}
                  title={tr('点开高级检索改筛选条件', 'Open advanced search to change the filters')}
                >
                  {tr('只看：', 'Showing: ')}
                  {[
                    announce === 'new' ? tr('新工作', 'New') : announce === 'cross' ? tr('更新', 'Updated') : '',
                    category,
                    author,
                    affiliation,
                  ]
                    .filter(Boolean)
                    .join(' · ')}
                </div>
              )}

              {semantic && (
                <div style={{ marginTop: 6, fontSize: 11, color: 'var(--text-4)', lineHeight: 1.5 }}>
                  {tr(
                    '语义检索只覆盖已生成向量的论文，结果可能不全。',
                    'Semantic search only covers papers that already have embeddings — results may be incomplete.',
                  )}
                </div>
              )}
              {fallbackNotice && (
                <div
                  style={{
                    marginTop: 6,
                    fontSize: 11,
                    color: 'var(--warn-tx)',
                    background: 'var(--warn-bg)',
                    borderRadius: 7,
                    padding: '5px 9px',
                    lineHeight: 1.5,
                  }}
                >
                  {tr('语义检索暂不可用，已回退为关键词匹配。', 'Semantic search unavailable — fell back to keyword matching.')}
                </div>
              )}
              {/* —— 日期步进（主导航，不收进高级检索） —— */}
              <div className="row gap6 wrap" style={{ marginTop: 8 }}>
                <button
                  className="btn btn-ghost sm"
                  style={{ padding: '0 7px', height: 24, fontSize: 11 }}
                  disabled={!canPrevDay}
                  onClick={goPrevDay}
                >
                  ‹ {tr('前一天', 'Prev day')}
                </button>
                <span
                  className={`chip${day !== null ? ' on' : ''}`}
                  title={
                    day !== null
                      ? tr('点击看全部 7 天', 'Click to show all 7 days')
                      : tr('点击只看最新一天', 'Click to show the latest day only')
                  }
                  onClick={() => pickDay(day === null ? latestDate : null)}
                >
                  {day !== null ? dayLabel(day, dayCount.get(day)) : tr('全部 7 天', 'All 7 days')}
                </span>
                <button
                  className="btn btn-ghost sm"
                  style={{ padding: '0 7px', height: 24, fontSize: 11 }}
                  disabled={!canNextDay}
                  onClick={goNextDay}
                >
                  {tr('后一天', 'Next day')} ›
                </button>
              </div>
            </div>

            <div className="scroll" style={{ overflowY: 'auto', flex: 1 }}>
              {listQuery.isLoading ? (
                <div className="empty">{tr('加载论文…', 'Loading papers…')}</div>
              ) : listQuery.isError ? (
                <EmptyState
                  compact
                  icon="x"
                  title={tr('无法加载每日新论文', 'Failed to load daily papers')}
                  desc={tr('后端不可用或接口尚未就绪，稍后重试。', 'Backend unavailable — try again later.')}
                />
              ) : items.length === 0 ? (
                <EmptyState
                  compact
                  icon="book"
                  title={filtered ? tr('没有匹配的论文', 'No matching papers') : tr('今天还没有新论文', 'No new papers yet')}
                  desc={
                    filtered
                      ? tr('换个关键词或过滤条件试试。', 'Try a different keyword or filter.')
                      : tr(
                          '今天还没有新论文。arxiv 周末不发布新提交。',
                          'No new papers yet. arxiv does not announce on weekends.',
                        )
                  }
                />
              ) : (
                items.map((p, i) => {
                  // 与上一条日期不同 → 插入粘性日期头
                  const prev = items[i - 1];
                  const newDay = i === 0 || prev?.feed_date !== p.feed_date;
                  return (
                    <Fragment key={p.entry_id}>
                      {newDay && (
                        <div
                          style={{
                            position: 'sticky',
                            top: 0,
                            zIndex: 3,
                            padding: '6px 16px',
                            fontSize: 11,
                            fontWeight: 700,
                            color: 'var(--text-3)',
                            background: 'var(--surface-2)',
                            borderBottom: '0.5px solid var(--border)',
                          }}
                        >
                          {dayLabel(p.feed_date, dayCount.get(p.feed_date))}
                        </div>
                      )}
                      <DailyRow
                        p={p}
                        active={p.entry_id === selectedId}
                        checked={selected.has(p.paper_id)}
                        selectMode={selectMode}
                        onClick={() => setSelectedId(p.entry_id)}
                        onToggleCheck={() => toggleSelected(p.paper_id)}
                      />
                    </Fragment>
                  );
                })
              )}
            </div>

            {pages > 1 && (
              <div
                className="row gap8"
                style={{ padding: '8px 14px', borderTop: '0.5px solid var(--border)', justifyContent: 'center' }}
              >
                <button className="btn btn-ghost sm" disabled={page <= 1} onClick={() => setPage((x) => x - 1)}>
                  {tr('上一页', 'Prev')}
                </button>
                <span className="mono" style={{ fontSize: 11, color: 'var(--text-3)' }}>
                  {page} / {pages}
                </span>
                <button className="btn btn-ghost sm" disabled={page >= pages} onClick={() => setPage((x) => x + 1)}>
                  {tr('下一页', 'Next')}
                </button>
              </div>
            )}

            {/* —— 底部固定操作栏：多选 + 导出引用 —— */}
            <div
              className="row gap8"
              style={{ padding: '9px 14px', borderTop: '0.5px solid var(--border)', flexShrink: 0 }}
            >
              <button
                className={'btn sm ' + (selectMode ? 'btn-primary' : 'btn-ghost')}
                title={tr('开启后列表出现复选框，可批量导出引用', 'Show checkboxes to export citations in bulk')}
                onClick={() => {
                  setSelectMode((m) => !m);
                  setSelected(new Set());
                }}
              >
                <Icon name="check" size={13} />
                {selectMode ? tr(`已选 ${selected.size}`, `${selected.size} selected`) : tr('多选', 'Select')}
              </button>
              {selectMode && (
                <button
                  className="btn btn-ghost sm"
                  disabled={selected.size === 0 || exportMutation.isPending}
                  onClick={() => exportMutation.mutate()}
                >
                  <Icon name="download" size={12} />
                  {tr('导出 BibTeX', 'Export BibTeX')}
                </button>
              )}
            </div>
          </div>

          {/* —— 右：详情 —— */}
          <div className="split-detail">
            {selectedId ? (
              <DailyDetailPane
                /* 换论文就重挂载：阅览模式、概念折叠等本地开合状态自动归位 */
                key={selectedId}
                entryId={selectedId}
                onCollect={openCollect}
                downloading={downloadPending.has(selectedId)}
                compiling={compilePending.has(selectedId)}
                onFetchPdf={fetchPdf}
                onCompile={compile}
                onFilterAuthor={filterByAuthor}
                onFilterAffiliation={filterByAffiliation}
              />
            ) : (
              <div className="empty" style={{ margin: 'auto' }}>
                {tr('选择论文查看详情', 'Select a paper to view details')}
              </div>
            )}
          </div>
        </div>
        )}
      </div>

      {collectPaper && (
        <CollectTreeModal
          paper={collectPaper}
          open={collectOpen}
          onClose={() => setCollectOpen(false)}
          onCollected={(t) => setProgress(t)}
        />
      )}

      {progress && (
        <PaperProgressModal
          taskId={progress.taskId}
          paperTitle={progress.title}
          onClose={() => setProgress(null)}
        />
      )}
    </div>
  );
}
