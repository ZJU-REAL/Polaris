import { Fragment, useCallback, useEffect, useRef, useState } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';
import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Icon } from '../../components/ui/Icon';
import { PageHead } from '../../components/ui/PageHead';
import { EmptyState } from '../../components/ui/EmptyState';
import { Segmented } from '../../components/ui/Segmented';
import { toast } from '../../components/ui/Toast';
import { ApiError, api, type DailyPaperItem, type DailySort, type PaperDetail } from '../../lib/api';
import { fmtTime } from '../../lib/format';
import { tr } from '../../lib/i18n';
import { Markdown } from '../../lib/markdown';
import { PaperReader } from '../wiki/PaperReader';
import { readerFrom } from '../reading/shared';
import { SearchInput, saveBlob, useDebounced } from '../wiki/shared';
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
}: {
  entryId: string;
  onCollect: (p: CollectPaperRef) => void;
}) {
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const location = useLocation();
  const [readerOpen, setReaderOpen] = useState(false);
  const { data: paper, isLoading, isError } = useQuery({
    queryKey: ['daily-paper', entryId],
    queryFn: () => api.getDailyPaper(entryId),
    retry: false,
  });

  // 下载原文：把 PDF 抓进平台（顺带抽全文/分块），成功后按钮变「阅读原文」
  const fetchPdfMutation = useMutation({
    mutationFn: () => api.requestPaperPdf(paper!.paper_id),
    onSuccess: () => {
      toast(tr('已下载原文，可以在线阅读了', 'PDF fetched — you can read it here now'), 'ok');
      void queryClient.invalidateQueries({ queryKey: ['daily-paper', entryId] });
    },
    onError: (e) =>
      toast(
        `${tr('下载原文失败', 'Failed to fetch the PDF')}：${e instanceof Error ? e.message : String(e)}`,
        'error',
      ),
  });

  // 单篇 AI 解读编译：同步等待（约半分钟）；409 = 已有人在编译
  const compileMutation = useMutation({
    mutationFn: () => api.compileDailyPaper(entryId),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['daily-paper', entryId] });
      void queryClient.invalidateQueries({ queryKey: ['daily-papers'] }); // 列表行的 has_wiki 标记
      void queryClient.invalidateQueries({ queryKey: ['daily-liked'] });
    },
    onError: (e) => {
      if (e instanceof ApiError && e.status === 409) {
        toast(tr('已有人在生成，稍后刷新即可', 'Someone is already generating it, refresh later'), 'info');
        void queryClient.invalidateQueries({ queryKey: ['daily-paper', entryId] });
      } else {
        toast(`${tr('生成解读失败', 'Failed to generate summary')}：${e instanceof Error ? e.message : String(e)}`, 'error');
      }
    },
  });

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

  const authors = paper.authors.map((a) => a.name).filter(Boolean).join(', ');
  // 「链接放 arxiv，不放标题」：优先原文 url，退回 arxiv abs 页
  const arxivHref = paper.url ?? (paper.arxiv_id ? `https://arxiv.org/abs/${paper.arxiv_id}` : null);
  // PDF 不可用时的下载去处：arxiv pdf，无 arxiv_id 退回原文 url
  const pdfDownloadUrl = paper.arxiv_id ? `https://arxiv.org/pdf/${paper.arxiv_id}` : paper.url;
  // 复用常规详情的阅览器：把每日详情映射成 PaperReader 需要的字段（id 用内容池 paper_id）
  const readerPaper: PaperDetail = {
    ...(paper as unknown as PaperDetail),
    id: paper.paper_id,
    venue: null,
    tldr: null,
  };

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
      {authors && <div style={{ fontSize: 12.5, color: 'var(--text-3)', marginBottom: 6 }}>{authors}</div>}
      {paper.published_at && (
        <div style={{ fontSize: 11.5, color: 'var(--text-4)', marginBottom: 14 }}>
          {tr('发布于', 'Published')} {fmtTime(paper.published_at)}
        </div>
      )}

      {/* —— 操作栏（对齐常规论文详情 PaperDetailPane） —— */}
      <div className="row gap8 wrap" style={{ marginBottom: 18 }}>
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
              disabled={fetchPdfMutation.isPending}
              onClick={() => fetchPdfMutation.mutate()}
              title={tr('下载 PDF 到平台，下载后即可在线阅读', 'Fetch the PDF into Polaris so it can be read here')}
            >
              {fetchPdfMutation.isPending ? (
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
          disabled={compileMutation.isPending}
          onClick={() => compileMutation.mutate()}
        >
          {compileMutation.isPending ? (
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
            onClick={() => setReaderOpen(true)}
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

      {paper.abstract ? (
        <div className="card card-pad" style={{ background: 'var(--surface-2)' }}>
          <div className="row gap8" style={{ marginBottom: 8 }}>
            <Icon name="file" size={14} style={{ color: 'var(--accent)' }} />
            <span style={{ fontSize: 12, fontWeight: 700 }}>{tr('摘要', 'Abstract')}</span>
          </div>
          <p style={{ fontSize: 13.5, lineHeight: 1.7, margin: 0 }}>{paper.abstract}</p>
        </div>
      ) : (
        <div className="empty" style={{ padding: 20 }}>{tr('这篇还没有摘要。', 'No abstract for this paper.')}</div>
      )}

      {/* —— AI 图文介绍：渲染风格对齐常规详情（同容器/字号/Markdown props） —— */}
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
            <span className="mono" style={{ fontSize: 11, color: 'var(--text-4)', letterSpacing: '0.04em' }}>
              {tr('AI 图文介绍', 'AI intro')}
            </span>
            <button
              className="btn btn-soft sm"
              title={tr('全屏专注阅读', 'Full-screen focused reading')}
              onClick={() => setReaderOpen(true)}
            >
              <Icon name="book" size={13} />
              {tr('阅览模式', 'Reading mode')}
            </button>
          </div>
          <Markdown source={paper.wiki_content} />
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
          renderFigure={() => null}
          onClose={() => setReaderOpen(false)}
        />
      )}
    </div>
  );
}

type DailyView = 'papers' | 'chat';
type AnnounceFilter = 'all' | 'new' | 'cross';
type SearchScope = 'keyword' | 'semantic';

export function DailyPage() {
  const [view, setView] = useState<DailyView>('papers');
  const [qInput, setQInput] = useState('');
  const q = useDebounced(qInput.trim());
  const [scope, setScope] = useState<SearchScope>('keyword');
  const [sort, setSort] = useState<DailySort>('likes');
  const [page, setPage] = useState(1);
  // —— 高级过滤：日期（null=全部 7 天，默认落在最新一天）/ 订阅分类（''=全部）/ 类型 ——
  const [day, setDay] = useState<string | null>(null);
  const [category, setCategory] = useState('');
  const [announce, setAnnounce] = useState<AnnounceFilter>('new');
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [collectPaper, setCollectPaper] = useState<CollectPaperRef | null>(null);
  const [collectOpen, setCollectOpen] = useState(false);
  // 收录到库/课题/个人后若启动了后台补全，弹出与手动添加同款分阶段进度框
  const [progress, setProgress] = useState<{ taskId: string; title: string } | null>(null);
  // 多选（批量导出引用）：默认关闭，底部「多选」按钮开启后行首出现复选框；存 paper_id
  const [selectMode, setSelectMode] = useState(false);
  const [selected, setSelected] = useState<Set<string>>(new Set());

  useEffect(() => setPage(1), [q, scope, sort, day, category, announce]);
  useEffect(() => {
    setSelected(new Set());
    setSelectMode(false);
  }, [q, scope, day, category, announce]);

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

  const categoriesQuery = useQuery({
    queryKey: ['daily-categories'],
    queryFn: () => api.getDailyCategories(),
    retry: false,
    staleTime: 300_000,
  });

  // 语义检索：只在有关键词时生效；结果按相关度排序、不分页
  const semantic = !!q && scope === 'semantic';
  const listQuery = useQuery({
    queryKey: ['daily-papers', scope, sort, page, q, day, category, announce],
    queryFn: () =>
      api.listDailyPapers({
        sort: semantic ? undefined : sort,
        page: semantic ? undefined : page,
        size: PAGE_SIZE,
        q: q || undefined,
        date: day ?? undefined,
        category: category || undefined,
        announce: announce === 'all' ? undefined : announce,
        mode: semantic ? 'semantic' : undefined,
      }),
    retry: false,
    placeholderData: keepPreviousData,
  });
  // 默认口径（当天 + 新工作）之外还加了条件才算「筛过」——空列表时给的话术不一样
  const filtered = !!q || !!category || announce !== 'new' || (day !== null && day !== latestDate);
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

  return (
    <div
      className="page fadeup"
      style={{ maxWidth: 1360, display: 'flex', flexDirection: 'column', height: '100%', paddingBottom: 24 }}
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
        className="card"
        style={{ overflow: 'hidden', display: 'flex', flexDirection: 'column', flex: 1, minHeight: 480 }}
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
                  placeholder={tr('搜标题 / 摘要 / 作者…', 'Search title / abstract / authors…')}
                />
                <span className="mono" style={{ fontSize: 10.5, color: 'var(--text-3)', flexShrink: 0 }}>
                  {total ? tr(`${total} 篇`, `${total}`) : ''}
                </span>
              </div>
              {/* 检索方式：关键词字面匹配 / 语义检索（按意思找） */}
              <div className="row gap6 wrap" style={{ marginTop: 10 }}>
                <span className={`chip${scope === 'keyword' ? ' on' : ''}`} onClick={() => setScope('keyword')}>
                  {tr('关键词', 'Keyword')}
                </span>
                <span className={`chip${scope === 'semantic' ? ' on' : ''}`} onClick={() => setScope('semantic')}>
                  {tr('语义检索', 'Semantic')}
                </span>
              </div>
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
              <div className="row gap6 wrap" style={{ marginTop: 8 }}>
                <span
                  className={`chip${!semantic && sort === 'likes' ? ' on' : ''}`}
                  style={semantic ? { opacity: 0.45, pointerEvents: 'none' } : undefined}
                  title={semantic ? tr('语义检索按相关度排序', 'Semantic results are ranked by relevance') : undefined}
                  onClick={() => setSort('likes')}
                >
                  {tr('按点赞', 'Most liked')}
                </span>
                <span
                  className={`chip${!semantic && sort === 'date' ? ' on' : ''}`}
                  style={semantic ? { opacity: 0.45, pointerEvents: 'none' } : undefined}
                  title={semantic ? tr('语义检索按相关度排序', 'Semantic results are ranked by relevance') : undefined}
                  onClick={() => setSort('date')}
                >
                  {tr('按时间', 'Newest')}
                </span>
                <span style={{ width: 1, height: 14, background: 'var(--border)', margin: '0 3px', flexShrink: 0 }} />
                {/* 类型筛选：全部 / 新工作(new) / 更新(cross) */}
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
              {/* —— 日期步进 + 订阅分类下拉 —— */}
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
                <div style={{ flex: 1 }} />
                <select
                  className="input mono"
                  style={{ height: 24, fontSize: 11, padding: '0 4px', maxWidth: 128, flexShrink: 0 }}
                  value={category}
                  onChange={(e) => setCategory(e.target.value)}
                >
                  <option value="">{tr('全部分类', 'All categories')}</option>
                  {(categoriesQuery.data?.categories ?? []).map((c) => (
                    <option key={c} value={c}>
                      {c}
                    </option>
                  ))}
                </select>
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
              <DailyDetailPane entryId={selectedId} onCollect={openCollect} />
            ) : (
              <div className="empty" style={{ margin: 'auto' }}>
                {tr('选择左侧论文查看详情', 'Select a paper to view details')}
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
