import { useCallback, useState, type CSSProperties } from 'react';
import { useMutation, useQuery, useQueryClient, type QueryKey } from '@tanstack/react-query';
import { Icon } from '../../components/ui/Icon';
import { EmptyState } from '../../components/ui/EmptyState';
import { Segmented } from '../../components/ui/Segmented';
import { toast } from '../../components/ui/Toast';
import { api, type MyMeta, type PaperConceptRef, type ReadingStatus } from '../../lib/api';
import { tr } from '../../lib/i18n';
import { NoteCard } from '../reading/NotesPanel';
import { READING_STATUS } from '../reading/shared';
import { categoryMeta } from '../wiki/shared';

/* ============================================================
   论文详情面板的公共小块：四处（共享文献库浏览 / 每日新论文 /
   课题相关研究 / 我的文献库）的数据源形状差别很大，所以不抽整块
   面板，只抽这几段跟数据源无关的 UI + 个人维度写操作。
   ============================================================ */

/** 概念 chips 默认最多展示数，超出折叠（与文献工作台一致）。 */
export const CONCEPT_CHIP_LIMIT = 12;

/* ---------------- 星标 + 阅读状态 ---------------- */

/**
 * 个人维度的两个开关：星标、阅读状态。
 * 走 PUT /papers/{id}/my-meta（对全员开放，只读浏览也能改）。
 */
export function PaperMyMetaRow({
  paperId,
  starred,
  readingStatus,
  detailKey,
  invalidateKeys,
  style,
}: {
  paperId: string;
  starred: boolean;
  readingStatus: ReadingStatus;
  /** 详情 queryKey：给了就直接改缓存，点完立刻变，不用等列表刷新 */
  detailKey?: QueryKey;
  /** 改完要刷新的列表 / 搜索 queryKey */
  invalidateKeys?: readonly QueryKey[];
  style?: CSSProperties;
}) {
  const queryClient = useQueryClient();

  const metaMutation = useMutation({
    mutationFn: (input: Partial<MyMeta>) => api.putMyMeta(paperId, input),
    onSuccess: (meta) => {
      if (detailKey) {
        queryClient.setQueryData<{ starred?: boolean; reading_status?: ReadingStatus }>(detailKey, (old) =>
          old ? { ...old, starred: meta.starred, reading_status: meta.reading_status } : old,
        );
      }
      for (const key of invalidateKeys ?? []) {
        void queryClient.invalidateQueries({ queryKey: key });
      }
    },
    onError: (e) =>
      toast(`${tr('更新失败：', 'Update failed: ')}${e instanceof Error ? e.message : String(e)}`, 'error'),
  });

  return (
    <div className="row gap12 wrap" style={{ marginTop: 12, ...style }}>
      <button
        className="btn btn-ghost sm"
        disabled={metaMutation.isPending}
        onClick={() => metaMutation.mutate({ starred: !starred })}
        style={starred ? { color: 'var(--warn-tx)' } : undefined}
      >
        <Icon name={starred ? 'starFill' : 'star'} size={13} />
        {starred ? tr('已星标', 'Starred') : tr('加星标', 'Star')}
      </button>
      <span className="row gap8">
        <span className="mono" style={{ fontSize: 10.5, color: 'var(--text-3)' }}>
          {tr('阅读状态', 'Reading status')}
        </span>
        <Segmented<ReadingStatus>
          options={READING_STATUS.map((m) => ({ v: m.v, label: tr(m.label, m.en) }))}
          value={readingStatus}
          onChange={(v) => metaMutation.mutate({ reading_status: v })}
        />
      </span>
    </div>
  );
}

/* ---------------- 标签：库标签（只读）+ 我的标签（可编辑） ---------------- */

/* 两组标签是两回事，样式上一眼分开：
   - 库标签＝实心底色（.tag 默认样式），整个文献库共用一套，编辑在文献工作台；
   - 我的标签＝描边 + 强调色，只有自己看得到，哪儿都能改。 */

/** 我的标签 chip 的描边样式（区别于实心的库标签）。 */
const myTagStyle: CSSProperties = {
  background: 'transparent',
  borderColor: 'var(--accent-soft-2)',
  color: 'var(--accent-text)',
  display: 'inline-flex',
  alignItems: 'center',
  gap: 4,
};

/** 库标签只读展示（编辑在文献工作台）；没有标签就不占位。 */
export function PaperTagsRow({ tags, style }: { tags: string[] | undefined; style?: CSSProperties }) {
  if (!tags || tags.length === 0) return null;
  return (
    <div className="row gap6 wrap" style={{ marginTop: 14, ...style }}>
      <span className="row gap6 mono" style={{ fontSize: 10.5, color: 'var(--text-3)', marginRight: 2 }}>
        <Icon name="users" size={11} />
        {tr('库标签', 'Library tags')}
      </span>
      {tags.map((t) => (
        <span key={t} className="tag">
          {t}
        </span>
      ))}
    </div>
  );
}

/**
 * 我的标签：就地增删，只有本人可见可改。
 * 走 PUT /papers/{id}/my-tags（整组覆盖），可达口径同 my-meta——只读浏览、
 * 每日新论文里也能打，不需要文献库的管理权限。
 */
export function PaperMyTagsRow({
  paperId,
  myTags,
  detailKey,
  invalidateKeys,
  style,
}: {
  paperId: string;
  myTags: string[] | undefined;
  /** 详情 queryKey：给了就直接改缓存，改完立刻变，不用等列表刷新 */
  detailKey?: QueryKey;
  /** 改完要刷新的列表 / 搜索 queryKey */
  invalidateKeys?: readonly QueryKey[];
  style?: CSSProperties;
}) {
  const queryClient = useQueryClient();
  const [adding, setAdding] = useState(false);
  const [value, setValue] = useState('');
  const tags = myTags ?? [];

  const putMutation = useMutation({
    mutationFn: (names: string[]) => api.putMyTags(paperId, names),
    onSuccess: (res) => {
      if (detailKey) {
        queryClient.setQueryData<{ my_tags?: string[] }>(detailKey, (old) =>
          old ? { ...old, my_tags: res.my_tags } : old,
        );
      }
      void queryClient.invalidateQueries({ queryKey: ['my-tags'] });
      for (const key of invalidateKeys ?? []) {
        void queryClient.invalidateQueries({ queryKey: key });
      }
    },
    onError: (e) =>
      toast(`${tr('标签更新失败：', 'Tag update failed: ')}${e instanceof Error ? e.message : String(e)}`, 'error'),
  });

  const commit = () => {
    const name = value.trim();
    setAdding(false);
    setValue('');
    if (!name || tags.includes(name)) return;
    putMutation.mutate([...tags, name]);
  };

  return (
    <div className="row gap6 wrap" style={{ marginTop: 10, ...style }}>
      <span
        className="row gap6 mono"
        style={{ fontSize: 10.5, color: 'var(--text-3)', marginRight: 2 }}
        title={tr('只有你自己看得到', 'Only you can see these')}
      >
        <Icon name="bookmark" size={11} />
        {tr('我的标签', 'My tags')}
      </span>
      {tags.map((t) => (
        <span key={t} className="tag" style={myTagStyle}>
          {t}
          <span
            title={tr('移除标签', 'Remove tag')}
            style={{ cursor: 'pointer', display: 'inline-flex', opacity: 0.6 }}
            onClick={() => putMutation.mutate(tags.filter((x) => x !== t))}
          >
            <Icon name="x" size={9} />
          </span>
        </span>
      ))}
      {adding ? (
        <input
          className="input"
          autoFocus
          style={{ height: 24, fontSize: 11.5, width: 120, padding: '0 8px' }}
          placeholder={tr('标签名，回车确定', 'Tag name, Enter to confirm')}
          value={value}
          onChange={(e) => setValue(e.target.value)}
          onBlur={commit}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !e.nativeEvent.isComposing) commit();
            if (e.key === 'Escape') {
              setAdding(false);
              setValue('');
            }
          }}
        />
      ) : (
        <span
          className="chip"
          style={{ fontSize: 11, height: 20, opacity: putMutation.isPending ? 0.5 : 1 }}
          onClick={() => !putMutation.isPending && setAdding(true)}
        >
          <Icon name="plus" size={10} style={{ display: 'inline-block', verticalAlign: -1 }} />{' '}
          {tr('加标签', 'Add tag')}
        </span>
      )}
    </div>
  );
}

/** 列表行里的标签小 chip：库标签实心 / 我的标签描边，最多各显 2 个。 */
export function PaperTagChips({
  tags,
  myTags,
  limit = 2,
}: {
  tags: string[] | undefined;
  myTags: string[] | undefined;
  limit?: number;
}) {
  const lib = tags ?? [];
  const mine = myTags ?? [];
  const hidden = Math.max(0, lib.length - limit) + Math.max(0, mine.length - limit);
  const chipStyle: CSSProperties = {
    fontSize: 10,
    height: 17,
    padding: '0 6px',
    maxWidth: 90,
    overflow: 'hidden',
    textOverflow: 'ellipsis',
    whiteSpace: 'nowrap',
    display: 'inline-block',
    lineHeight: '17px',
  };
  return (
    <>
      {lib.slice(0, limit).map((t) => (
        <span key={`lib-${t}`} className="tag" style={chipStyle} title={tr(`库标签：${t}`, `Library tag: ${t}`)}>
          {t}
        </span>
      ))}
      {mine.slice(0, limit).map((t) => (
        <span
          key={`my-${t}`}
          className="tag"
          style={{ ...chipStyle, background: 'transparent', borderColor: 'var(--accent-soft-2)', color: 'var(--accent-text)' }}
          title={tr(`我的标签：${t}`, `My tag: ${t}`)}
        >
          {t}
        </span>
      ))}
      {hidden > 0 && (
        <span className="mono" style={{ fontSize: 10, color: 'var(--text-4)' }}>
          +{hidden}
        </span>
      )}
    </>
  );
}

/* ---------------- 我的笔记 ---------------- */

/* GET/POST /papers/{id}/notes 对全员开放（内容池可读即可写自己的笔记），
   所以只读浏览、每日新论文里也能记笔记。后端只返回本人的笔记，因此列出来的每条都可以改/删。
   列表按需拉取（展开才请求），收起时计数用详情里的 note_count（同样是本人条数）。 */
export function PaperNotesSection({
  paperId,
  noteCount,
  invalidateKeys,
}: {
  paperId: string;
  noteCount: number;
  /** 笔记增删改后要刷新的 queryKey（详情 note_count / 列表行 / 库笔记本…） */
  invalidateKeys?: readonly QueryKey[];
}) {
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);
  const [draft, setDraft] = useState('');

  const notesQuery = useQuery({
    queryKey: ['paper-notes', paperId],
    queryFn: () => api.listPaperNotes(paperId),
    enabled: open,
    retry: false,
  });
  const notes = notesQuery.data ?? [];

  const refresh = useCallback(() => {
    void queryClient.invalidateQueries({ queryKey: ['paper-notes', paperId] });
    for (const key of invalidateKeys ?? []) {
      void queryClient.invalidateQueries({ queryKey: key });
    }
  }, [queryClient, paperId, invalidateKeys]);

  const createMutation = useMutation({
    mutationFn: () => api.createPaperNote(paperId, draft.trim()),
    onSuccess: () => {
      toast(tr('笔记已保存', 'Note saved'), 'ok');
      setDraft('');
      refresh();
    },
    onError: (e) =>
      toast(`${tr('保存失败：', 'Save failed: ')}${e instanceof Error ? e.message : String(e)}`, 'error'),
  });

  const count = notesQuery.data ? notes.length : noteCount;

  return (
    <div className="card" style={{ marginTop: 18, overflow: 'hidden' }}>
      <div
        className="row"
        onClick={() => setOpen((o) => !o)}
        style={{ padding: '11px 16px', cursor: 'pointer', justifyContent: 'space-between', userSelect: 'none' }}
      >
        <span className="row gap6" style={{ fontSize: 12.5, fontWeight: 650 }}>
          <Icon name="pen" size={12} style={{ color: 'var(--text-3)' }} />
          {tr('我的笔记', 'My notes')}
          <span className="mono" style={{ fontSize: 11, color: 'var(--text-3)', fontWeight: 400 }}>
            · {count}
          </span>
        </span>
        <Icon
          name="chevDown"
          size={14}
          style={{ color: 'var(--text-3)', transform: open ? 'rotate(180deg)' : 'none', transition: 'transform .15s' }}
        />
      </div>
      {open && (
        <div style={{ padding: '0 16px 14px' }}>
          {notesQuery.isLoading ? (
            <div className="empty" style={{ padding: 12 }}>
              {tr('加载笔记…', 'Loading notes…')}
            </div>
          ) : notesQuery.isError ? (
            <EmptyState
              compact
              icon="x"
              title={tr('笔记暂时加载不出来', 'Notes failed to load')}
              desc={tr('后端不可用或接口尚未就绪，稍后再试。', 'Backend unavailable or API not ready — try again later.')}
            />
          ) : notes.length === 0 ? (
            <div className="empty" style={{ padding: '4px 0 12px', textAlign: 'left' }}>
              {tr('还没有笔记，在下面写第一条。', 'No notes yet — write your first one below.')}
            </div>
          ) : (
            notes.map((n) => <NoteCard key={n.id} note={n} canEdit onSaved={refresh} />)
          )}
          <textarea
            className="textarea"
            style={{ width: '100%', minHeight: 72, maxHeight: 200, fontSize: 12.5, resize: 'vertical' }}
            placeholder={tr('记下想法、疑问或要点，支持 Markdown…', 'Jot down ideas, questions or key points — Markdown supported…')}
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
          />
          <div className="row" style={{ marginTop: 8, justifyContent: 'space-between', alignItems: 'center' }}>
            <span style={{ fontSize: 10.5, color: 'var(--text-4)' }}>
              {tr('笔记只有你自己看得到。', 'Only you can see your notes.')}
            </span>
            <button
              className="btn btn-primary sm"
              disabled={createMutation.isPending || !draft.trim()}
              onClick={() => createMutation.mutate()}
            >
              <Icon name="pen" size={13} />
              {createMutation.isPending ? tr('保存中…', 'Saving…') : tr('保存笔记', 'Save note')}
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

/* ---------------- 概念 chips ---------------- */

/**
 * 论文已上链的概念 chips，超过 limit 折叠。
 * onOpen 不传 = 不可点（每日新论文没有概念库上下文，只展示）。
 */
export function ConceptChips({
  concepts,
  onOpen,
  limit = CONCEPT_CHIP_LIMIT,
  style,
}: {
  concepts: PaperConceptRef[] | undefined;
  /** 点 chip → 打开概念页（有的页面按 id 跳、有的按名字跳，所以整个概念传出去）；不传则 chips 不可点 */
  onOpen?: (concept: PaperConceptRef) => void;
  limit?: number;
  style?: CSSProperties;
}) {
  const [open, setOpen] = useState(false);
  const list = concepts ?? [];
  if (list.length === 0) return null;
  const shown = open ? list : list.slice(0, limit);

  return (
    <div className="row gap8 wrap" style={{ marginTop: 16, ...style }}>
      {shown.map((c) => {
        const meta = categoryMeta(c.category);
        const label = (
          <>
            {c.name}
            <span style={{ opacity: 0.6, marginLeft: 5, fontSize: '0.85em' }}>{tr(meta.zh, meta.en)}</span>
          </>
        );
        return onOpen ? (
          <span
            key={c.id}
            className="wikilink"
            style={{ background: meta.bg, color: meta.c, height: 24 }}
            onClick={() => onOpen(c)}
          >
            {label}
          </span>
        ) : (
          <span
            key={c.id}
            className="pill sm"
            style={{ background: meta.bg, color: meta.c, height: 24 }}
            title={tr(meta.zh, meta.en)}
          >
            {label}
          </span>
        );
      })}
      {list.length > limit && (
        <span className="chip" style={{ fontSize: 11 }} onClick={() => setOpen((o) => !o)}>
          {open
            ? tr('收起', 'Collapse')
            : tr(`+${list.length - limit} 个概念`, `+${list.length - limit} concepts`)}
        </span>
      )}
    </div>
  );
}

/* ---------------- 阅览模式 / 导出 PDF ---------------- */

/** AI 图文介绍标题行右侧那对按钮（阅览模式 + 导出 PDF）。 */
export function WikiHeaderActions({
  onRead,
  onExport,
  style,
}: {
  /** 打开全屏阅览 */
  onRead: () => void;
  /** 打开阅览并唤起打印 */
  onExport: () => void;
  style?: CSSProperties;
}) {
  return (
    <div className="row gap6" style={style}>
      <button className="btn btn-soft sm" title={tr('全屏专注阅读', 'Full-screen focused reading')} onClick={onRead}>
        <Icon name="book" size={13} />
        {tr('阅览模式', 'Reading mode')}
      </button>
      <button
        className="btn btn-ghost sm"
        title={tr('打开阅览页并唤起打印，另存为 PDF', 'Open the reader and print to save as PDF')}
        onClick={onExport}
      >
        <Icon name="download" size={13} />
        {tr('导出 PDF', 'Export PDF')}
      </button>
    </div>
  );
}
