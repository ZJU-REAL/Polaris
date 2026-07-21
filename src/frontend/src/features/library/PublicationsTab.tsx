import { useEffect, useRef, useState } from 'react';
import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Icon } from '../../components/ui/Icon';
import { EmptyState } from '../../components/ui/EmptyState';
import { FormField } from '../../components/ui/FormField';
import { Modal } from '../../components/ui/Modal';
import { Segmented } from '../../components/ui/Segmented';
import { toast } from '../../components/ui/Toast';
import {
  api,
  ApiError,
  type AuthorCandidate,
  type AuthorProfile,
  type PaperAuthor,
  type Publication,
} from '../../lib/api';
import { fmtRelative } from '../../lib/format';
import { tr } from '../../lib/i18n';

/* ============================================================
   「我发表的」tab（issue #109）：
   未绑定作者信息 → 绑定向导（姓名/机构 → OpenAlex 候选 → 这是我 / 跳过）；
   已绑定 → 绑定摘要 + 立即同步 + 待确认（是我的 / 不是我的）+ 已确认列表 + 手动添加。
   ============================================================ */

export const PUBLICATIONS_PAGE_SIZE = 20;

// 模块级常量不调 tr()：保留 zh/en 字段，渲染处再 tr
const SOURCE_LABELS: Record<string, { zh: string; en: string }> = {
  openalex: { zh: '自动同步', en: 'Auto sync' },
  manual: { zh: '手动添加', en: 'Manual' },
};

type AddKind = 'arxiv' | 'doi' | 'bibtex';
const ADD_KINDS: { v: AddKind; zh: string; en: string }[] = [
  { v: 'arxiv', zh: 'arXiv ID', en: 'arXiv ID' },
  { v: 'doi', zh: 'DOI', en: 'DOI' },
  { v: 'bibtex', zh: 'BibTeX', en: 'BibTeX' },
];

function errText(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

function sourceLabel(source: string): string {
  const m = SOURCE_LABELS[source];
  return m ? tr(m.zh, m.en) : source;
}

/** 去重合并（大小写不敏感，保留首次出现的写法，去空）。 */
function dedupe(list: (string | null | undefined)[]): string[] {
  const seen = new Set<string>();
  const out: string[] = [];
  for (const raw of list) {
    const s = (raw ?? '').trim();
    if (!s) continue;
    const key = s.toLowerCase();
    if (seen.has(key)) continue;
    seen.add(key);
    out.push(s);
  }
  return out;
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

/** venue · 年份 · 被引 · 来源 的元信息行。 */
function MetaLine({ pub, withSource }: { pub: Publication; withSource?: boolean }) {
  const bits = [
    [pub.venue, pub.year].filter(Boolean).join(' · '),
    pub.cited_by_count > 0 ? tr(`被引 ${pub.cited_by_count} 次`, `${pub.cited_by_count} citations`) : '',
    withSource ? sourceLabel(pub.source) : '',
  ].filter(Boolean);
  if (bits.length === 0) return null;
  return (
    <div className="mono" style={{ fontSize: 10.5, color: 'var(--text-4)', marginTop: 3 }}>
      {bits.join(' · ')}
    </div>
  );
}

/* ============================================================
   绑定向导：姓名+机构 → 候选实体 → 这是我 / 都不是跳过
   ============================================================ */

function BindWizard({
  profile,
  onDone,
  onCancel,
}: {
  /** 已有绑定时用于预填（修改绑定入口）；未绑定时为 null。 */
  profile: AuthorProfile | null;
  onDone: () => void;
  onCancel?: () => void;
}) {
  const queryClient = useQueryClient();
  const [name, setName] = useState(profile?.name_variants[0] ?? '');
  const [affiliation, setAffiliation] = useState(profile?.affiliations[0] ?? '');
  const [search, setSearch] = useState<{ name: string; affiliation: string } | null>(null);

  const candQuery = useQuery({
    queryKey: ['author-candidates', search],
    queryFn: () => api.listAuthorCandidates(search!.name, search!.affiliation || undefined),
    enabled: search !== null,
    retry: false,
  });
  const candidates = candQuery.data ?? [];

  const saveMutation = useMutation({
    mutationFn: async (cand: AuthorCandidate | null) => {
      const saved = await api.saveAuthorProfile({
        name_variants: cand ? dedupe([name, cand.display_name, ...cand.alternate_names]) : dedupe([name]),
        affiliations: cand
          ? dedupe([affiliation, ...cand.affiliations.slice(0, 3)])
          : dedupe([affiliation]),
        openalex_author_id: cand ? cand.openalex_author_id : null,
        orcid: cand?.orcid ?? profile?.orcid ?? null,
        auto_sync: profile?.auto_sync ?? true,
      });
      // 绑定到实体后立即触发一次同步；同步失败不阻断绑定本身
      let synced = false;
      if (saved.openalex_author_id) {
        try {
          await api.syncPublications();
          synced = true;
        } catch {
          /* 列表视图里还有「立即同步」可以重试 */
        }
      }
      return { saved, synced };
    },
    onSuccess: ({ saved, synced }) => {
      queryClient.setQueryData(['author-profile'], saved);
      void queryClient.invalidateQueries({ queryKey: ['author-profile'] });
      void queryClient.invalidateQueries({ queryKey: ['publications'] });
      toast(
        synced
          ? tr('绑定成功，正在同步你的发表…', 'Linked — syncing your publications…')
          : tr('绑定信息已保存', 'Author info saved'),
        'ok',
      );
      onDone();
    },
    onError: (e) => toast(`${tr('保存失败：', 'Save failed: ')}${errText(e)}`, 'error'),
  });

  const searching = candQuery.isFetching;

  return (
    <div className="card card-pad" style={{ maxWidth: 680 }}>
      <div className="section-h">
        <Icon name="users" size={15} />
        {tr('先告诉我们你是谁', 'First, tell us who you are')}
      </div>
      <p style={{ fontSize: 12.5, color: 'var(--text-2)', margin: '8px 0 16px', lineHeight: 1.6 }}>
        {tr(
          '填写论文署名用的姓名（和机构），我们会到 OpenAlex 找到对应的作者，之后就能自动同步你发表的论文。',
          'Enter the name (and affiliation) you publish under. We will match it to an OpenAlex author so your publications can be synced automatically.',
        )}
      </p>

      <form
        onSubmit={(e) => {
          e.preventDefault();
          const n = name.trim();
          if (n) setSearch({ name: n, affiliation: affiliation.trim() });
        }}
      >
        <FormField label="姓名" en="Name" hint={tr('论文署名用的英文名，如 San Zhang', 'The name on your papers, e.g. San Zhang')}>
          <input
            className="input"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="e.g. San Zhang"
            autoFocus
          />
        </FormField>
        <FormField label="机构（选填）" en="Affiliation (optional)" style={{ marginTop: 12 }}>
          <input
            className="input"
            value={affiliation}
            onChange={(e) => setAffiliation(e.target.value)}
            placeholder="e.g. Zhejiang University"
          />
        </FormField>
        <div className="row gap8" style={{ marginTop: 14 }}>
          <button className="btn btn-primary sm" type="submit" disabled={!name.trim() || searching}>
            <Icon name="search" size={13} />
            {searching ? tr('查找中…', 'Searching…') : tr('查找我的作者信息', 'Find my author entry')}
          </button>
          {onCancel && (
            <button type="button" className="btn btn-ghost sm" onClick={onCancel}>
              {tr('取消', 'Cancel')}
            </button>
          )}
        </div>
      </form>

      {/* —— 候选实体 —— */}
      {search && !searching && (
        <div style={{ marginTop: 20 }}>
          {candQuery.isError ? (
            <div className="field-error">{`${tr('查找失败：', 'Search failed: ')}${errText(candQuery.error)}`}</div>
          ) : (
            <>
              <div style={{ fontSize: 12.5, fontWeight: 650, color: 'var(--text-2)', marginBottom: 8 }}>
                {candidates.length > 0
                  ? tr(`找到 ${candidates.length} 个可能是你的作者，选一个：`, `Found ${candidates.length} possible matches — pick yours:`)
                  : tr('没找到匹配的作者。', 'No matching authors found.')}
              </div>
              <div className="col" style={{ gap: 8 }}>
                {candidates.map((c) => (
                  <div
                    key={c.openalex_author_id}
                    className="card"
                    style={{ padding: '10px 14px', display: 'flex', gap: 12, alignItems: 'center' }}
                  >
                    <div style={{ flex: 1, minWidth: 0 }}>
                      <div className="row gap8" style={{ minWidth: 0 }}>
                        <span style={{ fontSize: 13, fontWeight: 650 }}>{c.display_name}</span>
                        {c.orcid && (
                          <span className="mono" style={{ fontSize: 10.5, color: 'var(--text-4)' }}>
                            {c.orcid}
                          </span>
                        )}
                      </div>
                      {c.alternate_names.length > 0 && (
                        <div
                          title={c.alternate_names.join(' / ')}
                          style={{
                            fontSize: 11,
                            color: 'var(--text-4)',
                            marginTop: 2,
                            whiteSpace: 'nowrap',
                            overflow: 'hidden',
                            textOverflow: 'ellipsis',
                          }}
                        >
                          {c.alternate_names.join(' / ')}
                        </div>
                      )}
                      {c.affiliations.length > 0 && (
                        <div
                          title={c.affiliations.join(' · ')}
                          style={{
                            fontSize: 11.5,
                            color: 'var(--text-3)',
                            marginTop: 2,
                            whiteSpace: 'nowrap',
                            overflow: 'hidden',
                            textOverflow: 'ellipsis',
                          }}
                        >
                          {c.affiliations.join(' · ')}
                        </div>
                      )}
                      <div className="mono" style={{ fontSize: 10.5, color: 'var(--text-4)', marginTop: 3 }}>
                        {tr(`论文 ${c.works_count} 篇 · 被引 ${c.cited_by_count} 次`, `${c.works_count} works · ${c.cited_by_count} citations`)}
                      </div>
                    </div>
                    <button
                      className="btn btn-primary sm"
                      style={{ flexShrink: 0 }}
                      disabled={saveMutation.isPending}
                      onClick={() => saveMutation.mutate(c)}
                    >
                      <Icon name="check" size={13} />
                      {tr('这是我', 'This is me')}
                    </button>
                  </div>
                ))}
              </div>
              <button
                className="btn btn-ghost sm"
                style={{ marginTop: 12 }}
                disabled={saveMutation.isPending}
                onClick={() => saveMutation.mutate(null)}
              >
                {candidates.length > 0
                  ? tr('都不是 / 跳过，只按姓名保存', 'None of these — save name only')
                  : tr('跳过，只按姓名保存', 'Skip — save name only')}
              </button>
              <div className="field-hint" style={{ marginTop: 6 }}>
                {tr(
                  '只按姓名保存时无法自动同步发表，之后可以用「手动添加」补充。',
                  'Saving name only disables auto sync; you can still add publications manually.',
                )}
              </div>
            </>
          )}
        </div>
      )}
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
  // 同步是后台任务：202 后按钮保持短暂 loading，几秒后 invalidate 刷新列表
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
      toast(tr('同步已开始，稍后自动刷新列表', 'Sync started — the list will refresh shortly'), 'ok');
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
    onError: (e) => {
      if (e instanceof ApiError && e.status === 400) {
        toast(tr('还没关联到作者，请先在「修改绑定」里选中你的作者信息', 'No author linked yet — pick your author entry via Edit binding first'), 'error');
      } else {
        toast(`${tr('同步失败：', 'Sync failed: ')}${errText(e)}`, 'error');
      }
    },
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
        title={tr('绑定信息暂时加载不出来', 'Failed to load your author info')}
        desc={tr('后端不可用或接口尚未就绪，稍后再试。', 'Backend unavailable or API not ready — try again later.')}
        action={
          <button className="btn btn-soft sm" onClick={() => void profileQuery.refetch()}>
            {tr('重试', 'Retry')}
          </button>
        }
      />
    );
  }

  /* —— 未绑定 / 修改绑定 → 向导 —— */
  if (!bound || editing) {
    return (
      <BindWizard
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
      {/* —— 绑定摘要 + 操作 —— */}
      <div
        className="card"
        style={{ padding: '12px 16px', display: 'flex', alignItems: 'center', gap: 12, flexWrap: 'wrap' }}
      >
        <div style={{ flex: 1, minWidth: 220 }}>
          <div style={{ fontSize: 13.5, fontWeight: 650 }}>
            {profile.name_variants[0] ?? '—'}
            {profile.affiliations[0] && (
              <span style={{ color: 'var(--text-3)', fontWeight: 500 }}>{` · ${profile.affiliations[0]}`}</span>
            )}
          </div>
          <div className="mono" style={{ fontSize: 10.5, color: 'var(--text-4)', marginTop: 3 }}>
            {profile.last_synced_at
              ? tr(`上次同步：${fmtRelative(profile.last_synced_at)}`, `Last synced: ${fmtRelative(profile.last_synced_at)}`)
              : tr('尚未同步', 'Not synced yet')}
            {!profile.openalex_author_id && ` · ${tr('未关联作者，无法自动同步', 'No author linked — auto sync unavailable')}`}
          </div>
        </div>
        <div className="row gap8" style={{ flexShrink: 0 }}>
          {profile.openalex_author_id && (
            <button className="btn btn-soft sm" disabled={syncBusy} onClick={() => syncMutation.mutate()}>
              <Icon name="refresh" size={13} />
              {syncBusy ? tr('同步中…', 'Syncing…') : tr('立即同步', 'Sync now')}
            </button>
          )}
          <button className="btn btn-ghost sm" onClick={() => setEditing(true)}>
            {tr('修改绑定', 'Edit binding')}
          </button>
          <button className="btn btn-primary sm" onClick={() => setAddOpen(true)}>
            <Icon name="plus" size={13} />
            {tr('手动添加', 'Add manually')}
          </button>
        </div>
      </div>

      {/* —— 待确认 —— */}
      {pendingCount > 0 && (
        <section>
          <div className="section-h">{tr(`待确认（${pendingCount}）`, `To confirm (${pendingCount})`)}</div>
          <p style={{ fontSize: 12, color: 'var(--text-3)', margin: '4px 0 10px', lineHeight: 1.5 }}>
            {tr(
              '自动同步找到的、可能是你发表的论文，确认后会进入下面的列表。',
              'Papers found by sync that may be yours — confirm to add them to your list.',
            )}
          </p>
          <div className="col" style={{ gap: 8 }}>
            {pending.map((pub) => (
              <div
                key={pub.id}
                className="card"
                style={{ padding: '10px 14px', display: 'flex', gap: 12, alignItems: 'flex-start' }}
              >
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div
                    title={pub.title}
                    style={{
                      fontSize: 13,
                      fontWeight: 650,
                      whiteSpace: 'nowrap',
                      overflow: 'hidden',
                      textOverflow: 'ellipsis',
                    }}
                  >
                    {pub.title}
                  </div>
                  <AuthorsLine authors={pub.authors} variants={variants} />
                  <MetaLine pub={pub} withSource />
                </div>
                <div className="row gap8" style={{ flexShrink: 0 }}>
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
                </div>
              </div>
            ))}
          </div>
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
              desc={
                profile.openalex_author_id
                  ? tr(
                      '点「立即同步」拉取你的发表，或用「手动添加」补充。',
                      'Tap Sync now to fetch your publications, or add them manually.',
                    )
                  : tr('用「手动添加」把你的论文加进来。', 'Use Add manually to add your papers.')
              }
            />
          ) : (
            <div className="col" style={{ gap: 8 }}>
              {confirmed.map((pub) => {
                const openable = !!pub.url;
                return (
                  <div
                    key={pub.id}
                    className={`card${openable ? ' hoverable' : ''}`}
                    role={openable ? 'button' : undefined}
                    tabIndex={openable ? 0 : undefined}
                    onClick={() => {
                      if (pub.url) window.open(pub.url, '_blank', 'noopener');
                    }}
                    onKeyDown={(e) => {
                      if (openable && (e.key === 'Enter' || e.key === ' ')) {
                        e.preventDefault();
                        window.open(pub.url!, '_blank', 'noopener');
                      }
                    }}
                    style={{
                      padding: '10px 14px',
                      display: 'flex',
                      gap: 12,
                      alignItems: 'flex-start',
                      cursor: openable ? 'pointer' : 'default',
                    }}
                  >
                    <div style={{ flex: 1, minWidth: 0 }}>
                      <div className="row gap8" style={{ minWidth: 0 }}>
                        <span
                          title={pub.title}
                          style={{
                            fontSize: 13,
                            fontWeight: 650,
                            whiteSpace: 'nowrap',
                            overflow: 'hidden',
                            textOverflow: 'ellipsis',
                            minWidth: 0,
                          }}
                        >
                          {pub.title}
                        </span>
                        {openable && (
                          <Icon name="link" size={12} style={{ color: 'var(--text-4)', flexShrink: 0 }} />
                        )}
                      </div>
                      <AuthorsLine authors={pub.authors} variants={variants} />
                      <MetaLine pub={pub} />
                    </div>
                    <button
                      className="btn btn-ghost sm"
                      style={{ flexShrink: 0 }}
                      disabled={decideMutation.isPending}
                      title={tr('从我的发表里拿掉', 'Remove from my publications')}
                      onClick={(e) => {
                        e.stopPropagation();
                        decideMutation.mutate({ id: pub.id, action: 'reject' });
                      }}
                    >
                      {tr('移除', 'Remove')}
                    </button>
                  </div>
                );
              })}
            </div>
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
