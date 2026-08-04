import { Fragment, useCallback, useEffect, useMemo, useState } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';
import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Icon } from '../../components/ui/Icon';
import { EmptyState } from '../../components/ui/EmptyState';
import {
  FigureEmbed,
  FiguresSection,
  hasEmbeddedFigures,
  usePaperFigures,
} from '../../components/ui/FigureGallery';
import { CompileBadge } from '../../components/ui/CompileBadge';
import { ConfirmModal } from '../../components/ui/ConfirmModal';
import { citationExportItems, ExportDropdown } from '../../components/ui/ExportDropdown';
import { Segmented } from '../../components/ui/Segmented';
import { toast } from '../../components/ui/Toast';
import { PaperIndexStatusRow } from '../../components/ui/PaperIndexStatus';
import { CollectingLibraries } from '../../components/ui/CollectingLibraries';
import {
  ApiError,
  api,
  type CitationFormat,
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
  MetaFold,
  MetaItem,
  SearchInput,
  SemanticSwitch,
  saveBlob,
  useDebounced,
  usePoolConceptNav,
} from '../wiki/shared';
import {
  ConceptChips,
  PaperMyMetaRow,
  PaperMyTagsRow,
  PaperNotesSection,
  WikiHeaderActions,
} from '../shared/PaperDetailBlocks';
import { DailyLikes } from './DailyLikes';
import { DailyChatTab } from './DailyChatTab';
import { CollectTreeModal, type CollectPaperRef } from './CollectTreeModal';
import { PaperProgressModal } from '../library/PaperProgressModal';

/* ============================================================
   /daily — 每日新论文：arxiv 每日新提交（订阅分类内）。保留期由管理员配置（默认 14 天）。
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
  // 已有解读时重新编译要先确认：解读每篇只有一份，重编即覆盖，旧版本找不回来
  const [recompileConfirm, setRecompileConfirm] = useState(false);
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

  // 每日推送是池级上下文：概念一律进不限库的概念页
  const { openConcept, openConceptByName } = usePoolConceptNav();

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
          onClick={() => (paper.has_wiki ? setRecompileConfirm(true) : onCompile(paper.entry_id))}
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

      {/* —— 我的标签：就地改，只有自己看得到 —— */}
      {/* 库标签的界面入口已移除，个人标签取代了它；后端端点与数据保留。 */}
      {poolPaper && (
        <PaperMyTagsRow
          paperId={poolPaper.id}
          myTags={poolPaper.my_tags}
          detailKey={poolKey}
          trailing={<PaperIndexStatusRow paperId={paper.paper_id} showRebuild={false} />}
        />
      )}

      {/* —— 哪些文献库收录了这篇：紧跟标签行，两者都是「这篇在平台里的归属」 —— */}
      <CollectingLibraries paperId={paper.paper_id} standalone />

      {/* —— 概念 chips（与库版同款；点了进不限库的概念页） —— */}
      <ConceptChips concepts={paper.concepts} onOpen={openConcept} />


      {/* 摘要默认折叠，复用元信息那套折叠块的样式，两者视觉一致 */}
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

      {/* 摘要 + 元信息合成一块：元信息本来就是查证时才看的东西，单独一张卡
          只是多一次点击。样式与「我的笔记」同款，默认收起。 */}
      <MetaFold label={tr('摘要', 'Abstract')}>
        {paper.abstract ? (
          <p style={{ fontSize: 13.5, lineHeight: 1.7, margin: 0 }}>{paper.abstract}</p>
        ) : (
          <p className="muted" style={{ fontSize: 12.5, margin: 0 }}>
            {tr('这篇还没有摘要。', 'No abstract for this paper.')}
          </p>
        )}
        <div style={{ marginTop: 14, paddingTop: 12, borderTop: '0.5px solid var(--border)' }}>
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
      
        </div>
      </MetaFold>

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
          <Markdown source={paper.wiki_content} onWikiLink={openConceptByName} renderFigure={renderFigure} />
        </div>
      ) : (
        <EmptyState
          compact
          icon="pen"
          title={tr('还没有 AI 介绍', 'No AI intro yet')}
          desc={tr(
            '点上方的编译按钮生成。',
            'Hit the compile button above to generate one.',
          )}
        />
      )}

      {readerOpen && (
        <PaperReader
          paper={readerPaper}
          wikiContent={paper.wiki_content}
          onWikiLink={openConceptByName}
          renderFigure={renderFigure}
          autoPrint={readerPrint}
          onClose={() => setReaderOpen(false)}
        />
      )}

      {/* 覆盖确认：解读每篇一份，重编会顶掉现有那份，且没有历史版本 */}
      <ConfirmModal
        open={recompileConfirm}
        onClose={() => setRecompileConfirm(false)}
        title={tr('重新编译解读', 'Recompile the wiki')}
        message={tr(
          `现有解读由 ${paper.compiled_by_name ?? '未知用户'} 在 ${paper.compiled_at ? fmtTime(paper.compiled_at) : '未知时间'} 用 ${paper.wiki_model ?? '未知模型'} 编译，重新编译会覆盖它，旧的找不回来。`,
          `The current wiki was compiled by ${paper.compiled_by_name ?? 'an unknown user'} at ${paper.compiled_at ? fmtTime(paper.compiled_at) : 'an unknown time'} with ${paper.wiki_model ?? 'an unknown model'}. Recompiling overwrites it — the old one cannot be recovered.`,
        )}
        confirmText={tr('重新编译', 'Recompile')}
        danger
        onConfirm={() => {
          setRecompileConfirm(false);
          onCompile(paper.entry_id);
        }}
      />
    </div>
  );
}

type DailyView = 'papers' | 'chat';
type AnnounceFilter = 'collected' | 'all' | 'new' | 'cross';

// 类型筛选默认值：只看已收录的（进了文献库的才值得先看；「恢复默认」也回到这个值）
const DEFAULT_ANNOUNCE: AnnounceFilter = 'collected';

// 列表固定按点赞排序（没有排序切换 UI）；语义检索时后端按相关度排，忽略这个值
// 时间倒排：最新的在最前。按点赞排会让一篇被点过的旧论文压在当天新论文前面，
// 而「每日新论文」这个页面的主线本来就是时间。
const DAILY_SORT: DailySort = 'date';

/** 每日池的同步状况。池子是所有文献库的唯一供给——抓取失败会让全实验室当天颗粒无收，
    所以状态要摆在页面上，而不是只躺在任务日志里等人去翻。 */
function SyncStatusPill() {
  const navigate = useNavigate();
  const { data } = useQuery({
    queryKey: ['daily-sync-status'],
    queryFn: () => api.getDailySyncStatus(),
    retry: false,
    refetchInterval: 60_000,
  });
  if (!data) return null;

  const failed = data.failed_categories;
  const latest = data.latest_feed_date ?? '—';
  // 旧后端没有 feed_state：按老口径折算，不然会一律显示成「已跟上」
  const state = data.feed_state ?? (failed.length > 0 ? 'failed' : data.stale ? 'stalled' : 'fresh');

  // 已跟上、以及「arXiv 自己没发」——都不是问题，用常规灰字，不摆警告
  if (state === 'fresh' || state === 'quiet') {
    return (
      <span className="mono" style={{ fontSize: 11, color: 'var(--text-4)', marginLeft: 10 }}>
        {state === 'quiet'
          ? tr(`arXiv 未更新 · 最新 ${latest}`, `Nothing new on arXiv · latest ${latest}`)
          : tr(`已更新至 ${latest}`, `Updated to ${latest}`)}
      </span>
    );
  }

  // 今天该公告、还在等：也不是故障，说清楚在等什么
  if (state === 'waiting') {
    const probed =
      data.probe_attempts !== undefined && data.probe_max_attempts
        ? `（${data.probe_attempts}/${data.probe_max_attempts}）`
        : '';
    return (
      <span
        className="mono"
        style={{ fontSize: 11, color: 'var(--text-4)', marginLeft: 10 }}
        title={tr(
          'arXiv 每天约北京时间 12:00 放当天批次，平台从设定时刻起每 15 分钟探一次',
          'arXiv publishes the day’s batch around 04:00 UTC; the platform probes every 15 minutes from the configured time',
        )}
      >
        {tr(`等待今天的批次${probed} · 最新 ${latest}`, `Waiting for today’s batch${probed} · latest ${latest}`)}
      </span>
    );
  }

  const label =
    state === 'failed'
      ? tr(`${failed.join('、')} 抓取失败`, `${failed.join(', ')} failed to fetch`)
      : tr(`论文池已停更（最新 ${latest}）`, `Feed is stale (latest ${latest})`);
  return (
    <span
      className={data.last_run_id ? 'pill sm hoverable' : 'pill sm'}
      style={{ background: 'var(--warn-bg)', color: 'var(--warn-tx)', marginLeft: 10 }}
      title={
        state === 'failed'
          ? tr('这些分类当天的新论文不会进池，各文献库也就收不到', 'Those categories will not enter the pool today, so no library will receive them')
          : tr('该公告的日子已经过去，池子却没跟上——不是周末，也不是 arXiv 没发', 'A publishing day has passed without the pool catching up — not a weekend, and not arXiv having nothing')
      }
      onClick={data.last_run_id ? () => navigate(`/voyages/${data.last_run_id}`) : undefined}
    >
      {label}
      {data.last_run_id ? ' →' : ''}
    </span>
  );
}

export function DailyPage() {
  const queryClient = useQueryClient();
  const [view, setView] = useState<DailyView>('papers');
  const [qInput, setQInput] = useState('');
  const q = useDebounced(qInput.trim());
  // 语义检索开关（true=按意思检索，false=关键词字面匹配）
  const [semanticOn, setSemanticOn] = useState(false);
  const [page, setPage] = useState(1);
  // —— 日期（null=全部，默认就停在全部）留在工具栏；分类 / 类型收进高级检索面板 ——
  // 只看被某个文献库收录的论文（#218）。空 = 不限
  // 日期：勾上「全部」就是跨天一起看（默认）；取消勾选后在有数据的日期间前后切换。
  // 只在这些日期间跳，而不是任意日历日——中间没有公告的日子点进去是空的。
  const [showAll, setShowAll] = useState(false);
  const [day, setDay] = useState('');
  // 高级检索默认展开：分类 / 类型是常用筛选，藏起来用户找不到
  // 默认收起：日期切换已经在工具栏上，高级条件是偶尔才用的东西
  const [advOpen, setAdvOpen] = useState(false);
  const [category, setCategory] = useState('');
  const [announce, setAnnounce] = useState<AnnounceFilter>(DEFAULT_ANNOUNCE);
  // 作者 / 机构：手填，或在右栏详情里点作者名、点机构 chip 带进来
  const [authorInput, setAuthorInput] = useState('');
  const [affiliationInput, setAffiliationInput] = useState('');
  const author = useDebounced(authorInput.trim());
  const affiliation = useDebounced(affiliationInput.trim());
  // 高级条件是否偏离默认（决定高级检索按钮上的小圆点）
  // 分类/类型已上工具栏；高级检索只剩作者与机构
  const advActive = !!author || !!affiliation;
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [collectPaper, setCollectPaper] = useState<CollectPaperRef | null>(null);
  const [collectOpen, setCollectOpen] = useState(false);
  // 收录到库/课题/个人后若启动了后台补全，弹出与手动添加同款分阶段进度框
  const [progress, setProgress] = useState<{ taskId: string; title: string } | null>(null);
  // 多选（批量导出引用）：默认关闭，底部「多选」按钮开启后行首出现复选框；存 paper_id
  const [selectMode, setSelectMode] = useState(false);
  const [selected, setSelected] = useState<Set<string>>(new Set());

  useEffect(() => setPage(1), [q, semanticOn, category, announce, author, affiliation, showAll, day]);
  useEffect(() => {
    setSelected(new Set());
    setSelectMode(false);
  }, [q, semanticOn, category, announce, author, affiliation]);

  // 日期标签上的数字要跟着筛选走，否则选了某个分类之后标签仍显示全部篇数，
  // 看起来就像筛选根本没生效。
  // 保留期是管理员可配的，界面上不能写死「7 天」
  const daysQuery = useQuery({
    queryKey: ['daily-days', announce, category],
    queryFn: () =>
      api.listDailyDays({
        announce: announce === 'new' || announce === 'cross' ? announce : undefined,
        category: category || undefined,
        collected: announce === 'collected' || undefined,
      }),
    retry: false,
    staleTime: 60_000,
  });
  const dayCount = new Map((daysQuery.data ?? []).map((d) => [d.date, d.count] as const));
  // 有数据的日期，升序（旧 → 新）；前后切换只在这些日期间跳
  const dates = (daysQuery.data ?? []).map((d) => d.date).sort();
  const dayIdx = dates.indexOf(day);

  // 默认停在最近一天。以前默认落到「今天」，周末没公告时页面是空的看着像坏了；
  // 落到「最新**有数据**的一天」没有这个问题——dates 本来就只含有数据的日期。
  useEffect(() => {
    const latest = dates[dates.length - 1];
    if (!showAll && !day && latest) setDay(latest);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [showAll, day, dates.join(',')]);


  // 默认停在「全部」：跨天一起看、按时间倒排，最新的自然在最前。以前默认落到最新
  // 一天，那一天恰好没有新公告（周末）时页面就是空的，看着像坏了。


  // 点作者/机构 → 列表只留匹配的论文（走已有的高级检索），其余条件重置并展开面板；
  // 日期放开到全部——只看当天的话，按作者/机构筛基本什么都剩不下
  const applyAdvFilter = useCallback(
    (patch: { author?: string; affiliation?: string }) => {
      setSemanticOn(false);
      setQInput('');
      setAuthorInput(patch.author ?? '');
      setAffiliationInput(patch.affiliation ?? '');
      setCategory('');
      setAnnounce('all');
      setAdvOpen(true);
      if (patch.author) {
        toast(tr(`已筛选作者：${patch.author}`, `Filtered by author: ${patch.author}`), 'info');
      } else if (patch.affiliation) {
        toast(tr(`已筛选机构：${patch.affiliation}`, `Filtered by affiliation: ${patch.affiliation}`), 'info');
      }
    },
    [],
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
    queryKey: [
      'daily-papers',
      semanticOn, page, q, category, announce, author, affiliation, showAll, day,
    ],
    queryFn: () =>
      api.listDailyPapers({
        sort: semantic ? undefined : DAILY_SORT,
        page: semantic ? undefined : page,
        size: PAGE_SIZE,
        q: q || undefined,
        category: category || undefined,
        announce: announce === 'new' || announce === 'cross' ? announce : undefined,
        collected: announce === 'collected' || undefined,
        author: author || undefined,
        affiliation: affiliation || undefined,
        date: showAll ? undefined : day || undefined,
        mode: semantic ? 'semantic' : undefined,
      }),
    retry: false,
    placeholderData: keepPreviousData,
  });
  // 默认口径（当天 + 新工作）之外还加了条件才算「筛过」——空列表时给的话术不一样
  // 选了某一天也算「筛过」——空列表时给的话术不一样
  const filtered = !!q || advActive || !showAll;
  const items = listQuery.data?.items ?? [];
  const total = listQuery.data?.total ?? 0;
  const pages = semantic ? 1 : Math.max(1, Math.ceil(total / PAGE_SIZE));
  // 后端明确回了「用的是关键词」→ 说明语义检索这会儿不可用
  const fallbackNotice = semantic && listQuery.data?.mode_used === 'keyword';

  // 首条自动选中
  // 默认选中列表第一篇，**并在列表变了之后跟着走**。
  // 原来只在 selectedId 为空时设一次：日期默认落到最新一天是异步的，列表先以「全部
  // 全部」返回，选中项就锁在了那一版的第一条——于是打开页面看到的是两天前那篇。
  const firstId = items[0]?.entry_id ?? null;
  const selectedInList = !!selectedId && items.some((p) => p.entry_id === selectedId);
  useEffect(() => {
    if (firstId && !selectedInList) setSelectedId(firstId);
  }, [firstId, selectedInList]);

  const toggleSelected = (paperId: string) =>
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(paperId)) next.delete(paperId);
      else next.add(paperId);
      return next;
    });

  const exportMutation = useMutation({
    mutationFn: (format: CitationFormat) => api.downloadDailyCitations({ format, ids: [...selected] }),
    onSuccess: (blob, format) => {
      saveBlob(blob, format === 'bibtex' ? 'polaris-daily-citations.bib' : 'polaris-daily-citations.json');
      toast(tr(`已导出 ${selected.size} 篇`, `Exported ${selected.size} papers`), 'ok');
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
      {/* —— 标签行：订阅分类并在同一行右侧 —— */}
      <div className="row page-tabs" style={{ marginBottom: 14 }}>
        <Segmented<DailyView>
          options={[
            { v: 'papers', label: tr('论文', 'Papers') },
            { v: 'chat', label: tr('对话', 'Chat') },
          ]}
          value={view}
          onChange={setView}
        />
        <SyncStatusPill />
        {(categoriesQuery.data?.categories.length ?? 0) > 0 && (
          <div
            className="row gap6 wrap"
            style={{ marginLeft: 'auto', justifyContent: 'flex-end', maxWidth: 420 }}
          >
            <span style={{ fontSize: 11, color: 'var(--text-4)' }}>{tr('订阅分类', 'Subscribed')}</span>
            {categoriesQuery.data?.categories.map((c) => (
              <span key={c} className="pill sm mono" style={{ background: 'var(--surface-3)' }}>
                {c}
              </span>
            ))}
          </div>
        )}
      </div>

      <div
        className="card split-card"
      >
        {view === 'chat' ? (
          /* ======== 池对话：就保留期内的每日新论文问答 ======== */
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
                  title={tr('高级检索：作者 / 机构', 'Advanced search: author / affiliation')}
                />
                <span className="mono" style={{ fontSize: 10.5, color: 'var(--text-3)', flexShrink: 0 }}>
                  {total ? tr(`${total} 篇`, `${total}`) : ''}
                </span>
              </div>

              {/* 分类与类型是主筛选，直接摆在工具栏上（作者/机构才收进高级检索） */}
              <div className="row gap6 wrap" style={{ marginTop: 8, alignItems: 'center' }}>
                <select
                  className="input mono"
                  style={{ width: 120, height: 26, fontSize: 11, padding: '0 6px', flexShrink: 0 }}
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
                <span
                  className={`chip${announce === 'collected' ? ' on' : ''}`}
                  onClick={() => setAnnounce('collected')}
                  title={tr('只看已被文献库收录的（默认）', 'Only papers collected into a library (default)')}
                >
                  {tr('已收录', 'Collected')}
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

              {/* 高级检索面板：作者 / 机构（分类与类型已上工具栏） */}
              {advOpen && (
                <AdvancedPanel
                  onClear={
                    advActive
                      ? () => {
                          setAuthorInput('');
                          setAffiliationInput('');
                        }
                      : undefined
                  }
                >
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
              {!advOpen && (!!author || !!affiliation) && (
                <div
                  onClick={() => setAdvOpen(true)}
                  style={{ marginTop: 6, fontSize: 11, color: 'var(--text-4)', cursor: 'pointer', lineHeight: 1.5 }}
                  title={tr('点开高级检索改筛选条件', 'Open advanced search to change the filters')}
                >
                  {tr('只看：', 'Showing: ')}
                  {[author, affiliation].filter(Boolean).join(' · ')}
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
              {/* —— 日期切换：前一天 / 当前 / 后一天，右侧勾选看全部 ——
                  只在有数据的日期间跳：中间没公告的日子（周末）点进去是空的。 */}
              <div className="row gap6 wrap" style={{ marginTop: 8, alignItems: 'center' }}>
                <button
                  className="btn btn-ghost sm"
                  style={{ padding: '0 7px', height: 24, fontSize: 11 }}
                  disabled={showAll || dayIdx <= 0}
                  onClick={() => setDay(dates[dayIdx - 1] ?? day)}
                  title={tr('前一天有公告的日期', 'Previous day with papers')}
                >
                  ‹ {tr('前一天', 'Prev')}
                </button>
                <span
                  className="mono"
                  style={{
                    minWidth: 132,
                    textAlign: 'center',
                    fontSize: 11.5,
                    color: showAll ? 'var(--text-4)' : 'var(--text-1)',
                    fontWeight: showAll ? 400 : 600,
                  }}
                >
                  {showAll
                    ? tr(`全部 ${dates.length} 天`, `All ${dates.length} days`)
                    : day
                      ? dayLabel(day, dayCount.get(day))
                      : tr('无数据', 'No data')}
                </span>
                <button
                  className="btn btn-ghost sm"
                  style={{ padding: '0 7px', height: 24, fontSize: 11 }}
                  disabled={showAll || dayIdx < 0 || dayIdx >= dates.length - 1}
                  onClick={() => setDay(dates[dayIdx + 1] ?? day)}
                  title={tr('后一天有公告的日期', 'Next day with papers')}
                >
                  {tr('后一天', 'Next')} ›
                </button>
                <label
                  className="row gap6"
                  style={{ alignItems: 'center', cursor: 'pointer', marginLeft: 4 }}
                  title={tr('跨天一起看，按时间倒排', 'Show every day, newest first')}
                >
                  <input
                    type="checkbox"
                    checked={showAll}
                    onChange={(e) => {
                      setShowAll(e.target.checked);
                      // 取消「全部」时落到最新一天，省得还要再点一次
                      if (!e.target.checked && !day) setDay(dates[dates.length - 1] ?? '');
                    }}
                  />
                  <span style={{ fontSize: 11.5, color: 'var(--text-3)' }}>{tr('全部', 'All')}</span>
                </label>
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
                          'arxiv 周末不发布新提交。',
                          'arxiv does not announce on weekends.',
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
                <ExportDropdown
                  sm
                  placement="up"
                  align="left"
                  busy={exportMutation.isPending}
                  disabled={selected.size === 0}
                  items={citationExportItems((format) => exportMutation.mutate(format))}
                />
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
