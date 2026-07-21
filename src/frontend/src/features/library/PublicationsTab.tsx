import { useEffect, useRef, useState, type ReactNode } from 'react';
import { useNavigate } from 'react-router-dom';
import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Icon } from '../../components/ui/Icon';
import { EmptyState } from '../../components/ui/EmptyState';
import { Modal } from '../../components/ui/Modal';
import { Segmented } from '../../components/ui/Segmented';
import { toast } from '../../components/ui/Toast';
import { api, ApiError, type PaperAuthor, type Publication } from '../../lib/api';
import { fmtRelative } from '../../lib/format';
import { tr } from '../../lib/i18n';
import { AuthorBindWizard, errText } from './AuthorBindWizard';

/* ============================================================
   「我发表的」tab（issue #109）：
   未填署名信息 → 表单（多个姓名写法 + 机构，保存即完成）；
   已填 → 署名摘要 + 立即扫描文献库 + 待确认（是我的 / 不是我的）
   + 已确认列表 + 手动添加；行样式对齐文献追踪的论文列表。
   ============================================================ */

export const PUBLICATIONS_PAGE_SIZE = 20;

// 模块级常量不调 tr()：保留 zh/en 字段，渲染处再 tr
const SOURCE_LABELS: Record<string, { zh: string; en: string }> = {
  openalex: { zh: '自动匹配', en: 'Auto matched' },
  library: { zh: '自动匹配', en: 'Auto matched' },
  manual: { zh: '手动添加', en: 'Manual' },
};

type AddKind = 'arxiv' | 'doi' | 'bibtex';
const ADD_KINDS: { v: AddKind; zh: string; en: string }[] = [
  { v: 'arxiv', zh: 'arXiv ID', en: 'arXiv ID' },
  { v: 'doi', zh: 'DOI', en: 'DOI' },
  { v: 'bibtex', zh: 'BibTeX', en: 'BibTeX' },
];

function sourceLabel(source: string): string {
  const m = SOURCE_LABELS[source];
  return m ? tr(m.zh, m.en) : source;
}

/** 作者名是否命中我的姓名写法（简单 includes 双向匹配）。 */
function nameMatches(name: string, variants: string[]): boolean {
  const n = name.trim().toLowerCase();
  if (!n) return false;
  return variants.some((v) => {
    const t = v.trim().toLowerCase();
    return t !== '' && (n.includes(t) || t.includes(n));
  });
}

/** 作者行：命中我的姓名写法的名字高亮。 */
function AuthorsLine({ authors, variants }: { authors: PaperAuthor[]; variants: string[] }) {
  if (authors.length === 0) return null;
  return (
    <div
      style={{
        fontSize: 11.5,
        color: 'var(--text-3)',
        marginTop: 3,
        whiteSpace: 'nowrap',
        overflow: 'hidden',
        textOverflow: 'ellipsis',
      }}
    >
      {authors.map((a, i) => (
        <span key={`${a.name}-${i}`}>
          {i > 0 && ', '}
          <span
            style={
              nameMatches(a.name, variants)
                ? { color: 'var(--accent-text)', fontWeight: 650 }
                : undefined
            }
          >
            {a.name}
          </span>
        </span>
      ))}
    </div>
  );
}

/* ============================================================
   发表行：与文献追踪论文列表同款版式（顶部 mono 元信息行 +
   标题 + 作者行 + 底部标签行），右侧放操作按钮。
   ============================================================ */

function PubRow({
  pub,
  variants,
  last,
  withSource,
  actions,
}: {
  pub: Publication;
  variants: string[];
  last: boolean;
  /** 底部标签行是否带来源（自动匹配 / 手动添加）。 */
  withSource?: boolean;
  actions: ReactNode;
}) {
  const navigate = useNavigate();
  // paper_id 非 null → 跳文献库阅读页；否则退回外链
  const openable = pub.paper_id !== null || !!pub.url;
  const open = () => {
    if (pub.paper_id) navigate(`/papers/${pub.paper_id}/read`);
    else if (pub.url) window.open(pub.url, '_blank', 'noopener');
  };
  return (
    <div
      className={openable ? 'list-hover' : undefined}
      role={openable ? 'button' : undefined}
      tabIndex={openable ? 0 : undefined}
      onClick={openable ? open : undefined}
      onKeyDown={(e) => {
        if (openable && (e.key === 'Enter' || e.key === ' ')) {
          e.preventDefault();
          open();
        }
      }}
      style={{
        padding: '12px 16px',
        borderBottom: last ? 'none' : '0.5px solid var(--border)',
        display: 'flex',
        gap: 12,
        alignItems: 'flex-start',
        cursor: openable ? 'pointer' : 'default',
      }}
    >
      <div style={{ flex: 1, minWidth: 0 }}>
        <div className="row gap8" style={{ marginBottom: 5 }}>
          <span className="mono" style={{ fontSize: 10.5, color: 'var(--text-3)' }}>
            {pub.arxiv_id ?? pub.venue ?? '—'}
          </span>
          {pub.year !== null && (
            <span className="mono" style={{ fontSize: 10.5, color: 'var(--text-4)' }}>
              {pub.year}
            </span>
          )}
          {pub.paper_id === null && pub.url && (
            <Icon name="link" size={11} style={{ color: 'var(--text-4)' }} />
          )}
        </div>
        <div style={{ fontSize: 13, fontWeight: 600, lineHeight: 1.35, color: 'var(--text)' }}>
          {pub.title}
        </div>
        <AuthorsLine authors={pub.authors} variants={variants} />
        <div className="row gap8" style={{ marginTop: 6 }}>
          {withSource && (
            <span className="pill sm" style={{ background: 'var(--surface-3)', color: 'var(--text-2)' }}>
              {sourceLabel(pub.source)}
            </span>
          )}
          {pub.venue && pub.arxiv_id && (
            <span
              style={{
                fontSize: 11.5,
                color: 'var(--text-3)',
                whiteSpace: 'nowrap',
                overflow: 'hidden',
                textOverflow: 'ellipsis',
                minWidth: 0,
              }}
            >
              {pub.venue}
            </span>
          )}
          {pub.cited_by_count > 0 && (
            <span className="mono" style={{ fontSize: 10.5, color: 'var(--text-4)', flexShrink: 0 }}>
              {tr(`被引 ${pub.cited_by_count} 次`, `${pub.cited_by_count} citations`)}
            </span>
          )}
        </div>
      </div>
      <div className="row gap6" style={{ flexShrink: 0 }} onClick={(e) => e.stopPropagation()}>
        {actions}
      </div>
    </div>
  );
}

/** 发表列表的包裹容器：圆角描边，内部行用分隔线（同垃圾桶列表）。 */
function PubList({ children }: { children: ReactNode }) {
  return (
    <div style={{ border: '0.5px solid var(--border)', borderRadius: 10, overflow: 'hidden' }}>
      {children}
    </div>
  );
}

/* ============================================================
   手动添加弹层：arXiv ID / DOI / BibTeX 三选一
   ============================================================ */

function AddPublicationModal({ open, onClose }: { open: boolean; onClose: () => void }) {
  const queryClient = useQueryClient();
  const [kind, setKind] = useState<AddKind>('arxiv');
  const [value, setValue] = useState('');
  const [error, setError] = useState<string | null>(null);

  const addMutation = useMutation({
    mutationFn: () => {
      const v = value.trim();
      const input = kind === 'arxiv' ? { arxiv_id: v } : kind === 'doi' ? { doi: v } : { bibtex: v };
      return api.addPublication(input);
    },
    onSuccess: () => {
      toast(tr('已添加到我的发表', 'Added to your publications'), 'ok');
      void queryClient.invalidateQueries({ queryKey: ['publications'] });
      setValue('');
      setError(null);
      onClose();
    },
    onError: (e) => {
      // 422 = 解析失败，detail 直接展示在弹层里
      if (e instanceof ApiError && e.status === 422) {
        setError(`${tr('解析失败：', 'Could not parse: ')}${e.message}`);
      } else {
        setError(errText(e));
      }
    },
  });

  return (
    <Modal
      open={open}
      onClose={onClose}
      title={tr('手动添加发表', 'Add a publication')}
      sub={tr('三选一：填 arXiv ID、DOI，或粘贴 BibTeX。', 'Provide one of: arXiv ID, DOI, or a BibTeX entry.')}
      footer={
        <>
          <button className="btn btn-ghost sm" onClick={onClose}>
            {tr('取消', 'Cancel')}
          </button>
          <button
            className="btn btn-primary sm"
            disabled={!value.trim() || addMutation.isPending}
            onClick={() => addMutation.mutate()}
          >
            {addMutation.isPending ? tr('添加中…', 'Adding…') : tr('添加', 'Add')}
          </button>
        </>
      }
    >
      <Segmented<AddKind>
        options={ADD_KINDS.map((k) => ({ v: k.v, label: tr(k.zh, k.en) }))}
        value={kind}
        onChange={(k) => {
          setKind(k);
          setValue('');
          setError(null);
        }}
      />
      <div style={{ marginTop: 12 }}>
        {kind === 'bibtex' ? (
          <textarea
            className="textarea"
            rows={7}
            value={value}
            onChange={(e) => {
              setValue(e.target.value);
              setError(null);
            }}
            placeholder={'@article{shen2026polaris,\n  title = {...},\n  ...\n}'}
            style={{ width: '100%', fontFamily: 'var(--mono)', fontSize: 12 }}
          />
        ) : (
          <input
            className="input"
            value={value}
            onChange={(e) => {
              setValue(e.target.value);
              setError(null);
            }}
            placeholder={kind === 'arxiv' ? '2401.12345' : '10.1145/3576915.3616613'}
            style={{ width: '100%' }}
          />
        )}
        {error && (
          <div className="field-error" style={{ marginTop: 6 }}>
            {error}
          </div>
        )}
      </div>
    </Modal>
  );
}

/* ============================================================
   主体
   ============================================================ */

export function PublicationsTab() {
  const queryClient = useQueryClient();
  const [editing, setEditing] = useState(false);
  const [addOpen, setAddOpen] = useState(false);
  const [confirmedPage, setConfirmedPage] = useState(1);
  // 扫描是后台任务：202 后按钮保持短暂 loading，几秒后 invalidate 刷新列表
  const [syncing, setSyncing] = useState(false);
  const timersRef = useRef<number[]>([]);
  useEffect(
    () => () => {
      timersRef.current.forEach((t) => window.clearTimeout(t));
    },
    [],
  );

  const profileQuery = useQuery({
    queryKey: ['author-profile'],
    queryFn: () => api.getAuthorProfile(),
    retry: false,
  });
  const profile = profileQuery.data ?? null;
  const bound = profile !== null;

  const pendingQuery = useQuery({
    queryKey: ['publications', 'pending', 1],
    queryFn: () => api.listPublications({ status: 'pending', page: 1, size: PUBLICATIONS_PAGE_SIZE }),
    enabled: bound,
    retry: false,
  });
  const confirmedQuery = useQuery({
    queryKey: ['publications', 'confirmed', confirmedPage],
    queryFn: () =>
      api.listPublications({ status: 'confirmed', page: confirmedPage, size: PUBLICATIONS_PAGE_SIZE }),
    enabled: bound,
    retry: false,
    placeholderData: keepPreviousData,
  });

  const pending = pendingQuery.data?.items ?? [];
  const pendingCount = pendingQuery.data?.counts.pending ?? 0;
  const confirmed = confirmedQuery.data?.items ?? [];
  const confirmedTotal = confirmedQuery.data?.total ?? 0;
  const confirmedPages = confirmedQuery.data
    ? Math.max(1, Math.ceil(confirmedQuery.data.total / confirmedQuery.data.size))
    : 1;

  const invalidatePubs = () => {
    void queryClient.invalidateQueries({ queryKey: ['publications'] });
  };

  const syncMutation = useMutation({
    mutationFn: () => api.syncPublications(),
    onSuccess: () => {
      setSyncing(true);
      toast(tr('扫描已开始，稍后自动刷新列表', 'Scan started — the list will refresh shortly'), 'ok');
      timersRef.current.push(
        window.setTimeout(() => {
          invalidatePubs();
          void queryClient.invalidateQueries({ queryKey: ['author-profile'] });
        }, 4000),
        window.setTimeout(() => {
          invalidatePubs();
          void queryClient.invalidateQueries({ queryKey: ['author-profile'] });
          setSyncing(false);
        }, 12000),
      );
    },
    onError: (e) => toast(`${tr('扫描失败：', 'Scan failed: ')}${errText(e)}`, 'error'),
  });

  const decideMutation = useMutation({
    mutationFn: ({ id, action }: { id: string; action: 'confirm' | 'reject' }) =>
      action === 'confirm' ? api.confirmPublication(id) : api.rejectPublication(id),
    onSuccess: (_d, v) => {
      toast(
        v.action === 'confirm' ? tr('已加入我的发表', 'Added to your publications') : tr('已移除', 'Removed'),
        'ok',
      );
      invalidatePubs();
    },
    onError: (e) => toast(`${tr('操作失败：', 'Action failed: ')}${errText(e)}`, 'error'),
  });

  /* —— 加载 / 出错 —— */
  if (profileQuery.isLoading) {
    return <div className="empty">{tr('加载中…', 'Loading…')}</div>;
  }
  if (profileQuery.isError) {
    return (
      <EmptyState
        compact
        icon="x"
        title={tr('署名信息暂时加载不出来', 'Failed to load your author info')}
        desc={tr('后端不可用或接口尚未就绪，稍后再试。', 'Backend unavailable or API not ready — try again later.')}
        action={
          <button className="btn btn-soft sm" onClick={() => void profileQuery.refetch()}>
            {tr('重试', 'Retry')}
          </button>
        }
      />
    );
  }

  /* —— 未填署名信息 / 修改 → 表单 —— */
  if (!bound || editing) {
    return (
      <AuthorBindWizard
        profile={profile}
        onDone={() => setEditing(false)}
        onCancel={bound ? () => setEditing(false) : undefined}
      />
    );
  }

  const variants = profile.name_variants;
  const syncBusy = syncMutation.isPending || syncing;

  return (
    <div className="col" style={{ gap: 18 }}>
      {/* —— 署名摘要 + 操作 —— */}
      <div
        className="card"
        style={{
          padding: '12px 16px',
          background: 'var(--surface-2)',
          display: 'flex',
          alignItems: 'center',
          gap: 12,
          flexWrap: 'wrap',
        }}
      >
        <div style={{ flex: 1, minWidth: 220 }}>
          <div style={{ fontSize: 13.5, fontWeight: 650 }}>
            {profile.name_variants.join(' / ') || '—'}
            {profile.affiliations.length > 0 && (
              <span style={{ color: 'var(--text-3)', fontWeight: 500 }}>
                {` · ${profile.affiliations.join(' · ')}`}
              </span>
            )}
          </div>
          <div className="mono" style={{ fontSize: 10.5, color: 'var(--text-4)', marginTop: 3 }}>
            {profile.last_synced_at
              ? tr(`上次匹配：${fmtRelative(profile.last_synced_at)}`, `Last matched: ${fmtRelative(profile.last_synced_at)}`)
              : tr('还没匹配过', 'Not matched yet')}
          </div>
        </div>
        <div className="row gap8" style={{ flexShrink: 0 }}>
          <button className="btn btn-soft sm" disabled={syncBusy} onClick={() => syncMutation.mutate()}>
            <Icon name="refresh" size={13} style={syncBusy ? { animation: 'spin 1s linear infinite' } : undefined} />
            {syncBusy ? tr('扫描中…', 'Scanning…') : tr('立即扫描文献库', 'Scan library now')}
          </button>
          <button className="btn btn-ghost sm" onClick={() => setEditing(true)}>
            {tr('修改署名信息', 'Edit author info')}
          </button>
          <button className="btn btn-primary sm" onClick={() => setAddOpen(true)}>
            <Icon name="plus" size={13} />
            {tr('手动添加', 'Add manually')}
          </button>
        </div>
        <div style={{ width: '100%', fontSize: 11, color: 'var(--text-4)', lineHeight: 1.5 }}>
          {tr('每天会自动匹配一次，命中的进入待确认', 'Runs automatically once a day; matches land in the pending list')}
        </div>
      </div>

      {/* —— 待确认 —— */}
      {pendingCount > 0 && (
        <section>
          <div className="section-h">{tr(`待确认（${pendingCount}）`, `To confirm (${pendingCount})`)}</div>
          <p style={{ fontSize: 12, color: 'var(--text-3)', margin: '4px 0 10px', lineHeight: 1.5 }}>
            {tr(
              '自动匹配到的、可能是你发表的论文，确认后会进入下面的列表。',
              'Papers matched automatically that may be yours — confirm to add them to your list.',
            )}
          </p>
          <PubList>
            {pending.map((pub, i) => (
              <PubRow
                key={pub.id}
                pub={pub}
                variants={variants}
                last={i === pending.length - 1}
                withSource
                actions={
                  <>
                    <button
                      className="btn btn-primary sm"
                      disabled={decideMutation.isPending}
                      onClick={() => decideMutation.mutate({ id: pub.id, action: 'confirm' })}
                    >
                      <Icon name="check" size={13} />
                      {tr('是我的', 'Mine')}
                    </button>
                    <button
                      className="btn btn-ghost sm"
                      disabled={decideMutation.isPending}
                      onClick={() => decideMutation.mutate({ id: pub.id, action: 'reject' })}
                    >
                      <Icon name="x" size={13} />
                      {tr('不是我的', 'Not mine')}
                    </button>
                  </>
                }
              />
            ))}
          </PubList>
        </section>
      )}

      {/* —— 已确认列表 —— */}
      <section>
        <div className="section-h">
          {tr(`我发表的论文（${confirmedTotal}）`, `My publications (${confirmedTotal})`)}
        </div>
        <div style={{ marginTop: 10 }}>
          {confirmedQuery.isLoading ? (
            <div className="empty">{tr('加载中…', 'Loading…')}</div>
          ) : confirmedQuery.isError ? (
            <EmptyState
              compact
              icon="x"
              title={tr('发表列表暂时加载不出来', 'Failed to load your publications')}
              action={
                <button className="btn btn-soft sm" onClick={() => void confirmedQuery.refetch()}>
                  {tr('重试', 'Retry')}
                </button>
              }
            />
          ) : confirmed.length === 0 ? (
            <EmptyState
              compact
              icon="file"
              title={tr('还没有已确认的发表', 'No confirmed publications yet')}
              desc={tr(
                '点「立即扫描文献库」找找你的论文，或用「手动添加」补充。',
                'Tap Scan library now to look for your papers, or add them manually.',
              )}
            />
          ) : (
            <PubList>
              {confirmed.map((pub, i) => (
                <PubRow
                  key={pub.id}
                  pub={pub}
                  variants={variants}
                  last={i === confirmed.length - 1}
                  actions={
                    <button
                      className="btn btn-ghost sm"
                      disabled={decideMutation.isPending}
                      title={tr('从我的发表里拿掉', 'Remove from my publications')}
                      onClick={() => decideMutation.mutate({ id: pub.id, action: 'reject' })}
                    >
                      {tr('移除', 'Remove')}
                    </button>
                  }
                />
              ))}
            </PubList>
          )}
        </div>

        {/* —— 分页 —— */}
        {confirmedTotal > PUBLICATIONS_PAGE_SIZE && (
          <div className="row gap12" style={{ justifyContent: 'center', marginTop: 16 }}>
            <button
              className="btn btn-ghost sm"
              disabled={confirmedPage <= 1}
              onClick={() => setConfirmedPage((p) => p - 1)}
            >
              <Icon name="chevron" size={12} style={{ transform: 'rotate(180deg)' }} />
              {tr('上一页', 'Prev')}
            </button>
            <span className="mono" style={{ fontSize: 11, color: 'var(--text-3)' }}>
              {tr(`第 ${confirmedPage} / ${confirmedPages} 页`, `Page ${confirmedPage} / ${confirmedPages}`)}
            </span>
            <button
              className="btn btn-ghost sm"
              disabled={confirmedPage >= confirmedPages}
              onClick={() => setConfirmedPage((p) => p + 1)}
            >
              {tr('下一页', 'Next')}
              <Icon name="chevron" size={12} />
            </button>
          </div>
        )}
      </section>

      <AddPublicationModal open={addOpen} onClose={() => setAddOpen(false)} />
    </div>
  );
}
