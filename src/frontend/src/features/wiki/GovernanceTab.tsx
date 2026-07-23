import { useEffect, useMemo, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Icon } from '../../components/ui/Icon';
import { toast } from '../../components/ui/Toast';
import { api, isAdmin, type DirectionLibraryDetail, type DuplicateCandidatePaper, type RubricDimension } from '../../lib/api';
import { tr } from '../../lib/i18n';

/* ============================================================
   文献库「治理」页签（P6）：
   - 库信息与预算编辑（可管理者：成员 / 文献库管理员 / 平台管理员）；
   - 本月 AI 用量进度（超限后同步任务暂停到下月）；
   - 文献库管理员名单（仅平台管理员可编辑）；
   - 重复论文候选与合并（不可撤销）。
   ============================================================ */

const CADENCES: { v: string; zh: string; en: string }[] = [
  { v: '', zh: '手动同步', en: 'Manual' },
  { v: 'daily', zh: '每天自动同步', en: 'Daily' },
];

export function GovernanceTab({ libraryId }: { libraryId: string }) {
  const { data: me } = useQuery({ queryKey: ['me'], queryFn: () => api.me(), retry: false, staleTime: 60_000 });
  const admin = isAdmin(me);

  const { data: lib } = useQuery({
    queryKey: ['library', libraryId],
    queryFn: () => api.getLibrary(libraryId),
    retry: false,
  });

  return (
    <div className="col gap16" style={{ padding: 20, overflowY: 'auto' }}>
      {lib && <LibraryInfoCard lib={lib} />}
      {lib && <InclusionSettingsCard lib={lib} />}
      <BudgetCard libraryId={libraryId} />
      <CuratorsCard libraryId={libraryId} canEdit={admin} />
      <DuplicatesCard libraryId={libraryId} />
    </div>
  );
}

/* —— 重复论文候选与合并 —— */

const REASON_LABEL: Record<string, { zh: string; en: string }> = {
  arxiv: { zh: '同一 arXiv 编号', en: 'Same arXiv id' },
  doi: { zh: '同一 DOI', en: 'Same DOI' },
  title: { zh: '标题相同', en: 'Same title' },
};

function DuplicatesCard({ libraryId }: { libraryId: string }) {
  const queryClient = useQueryClient();
  const { data: groups, isLoading, isError } = useQuery({
    queryKey: ['library-duplicates', libraryId],
    queryFn: () => api.listDuplicateCandidates(libraryId),
    retry: false,
  });

  const merge = useMutation({
    mutationFn: (input: { keep_id: string; drop_id: string }) => api.mergePapers(input),
    onSuccess: () => {
      toast(tr('已合并为一篇论文', 'Merged into one paper'), 'ok');
      void queryClient.invalidateQueries({ queryKey: ['library-duplicates', libraryId] });
      void queryClient.invalidateQueries({ queryKey: ['library', libraryId] });
      void queryClient.invalidateQueries({ queryKey: ['papers'] });
    },
    onError: () => toast(tr('合并失败，请重试', 'Merge failed, please retry'), 'error'),
  });

  function confirmMerge(keep: DuplicateCandidatePaper, drop: DuplicateCandidatePaper) {
    const ok = window.confirm(
      `${tr('确定合并这两篇论文？', 'Merge these two papers?')}\n\n` +
        `${tr('保留：', 'Keep: ')}${keep.title}\n${tr('并入后删除：', 'Merge & delete: ')}${drop.title}\n\n` +
        tr(
          '被删除那篇的解读、笔记、划线、收藏等会全部并到保留的那篇上。此操作不可撤销。',
          'Its wiki, notes, highlights and stars will all move to the kept paper. This cannot be undone.',
        ),
    );
    if (ok) merge.mutate({ keep_id: keep.id, drop_id: drop.id });
  }

  return (
    <section className="card" style={{ padding: 18 }}>
      <h3 style={{ fontSize: 14, fontWeight: 700, marginBottom: 6 }}>
        {tr('重复论文', 'Duplicate papers')}
      </h3>
      <p className="muted" style={{ fontSize: 12, marginBottom: 12 }}>
        {tr(
          '同一篇论文可能因预印本 / 正式版等原因入库两次。确认后合并：保留更完整的一篇，另一篇的所有内容并入后删除（不可撤销）。',
          'The same paper can be ingested twice (e.g. preprint vs published). Merging keeps the more complete row and folds the other into it — irreversible.',
        )}
      </p>
      {isLoading ? (
        <div className="skel" style={{ height: 48 }} />
      ) : isError ? (
        <div className="muted" style={{ fontSize: 13 }}>{tr('候选加载失败', 'Failed to load candidates')}</div>
      ) : !groups || groups.length === 0 ? (
        <div className="muted" style={{ fontSize: 13 }}>
          {tr('没有发现疑似重复的论文。', 'No suspected duplicates found.')}
        </div>
      ) : (
        <div className="col gap12">
          {groups.map((group, gi) => {
            const keep = group.papers[0];
            if (!keep) return null;
            return (
              <div
                key={`${group.reason}-${keep.id}-${gi}`}
                style={{ border: '1px solid var(--border)', borderRadius: 8, padding: 12 }}
              >
                <div className="muted" style={{ fontSize: 12, marginBottom: 8 }}>
                  {tr(REASON_LABEL[group.reason]?.zh ?? group.reason, REASON_LABEL[group.reason]?.en ?? group.reason)}
                </div>
                <div className="col gap8">
                  {group.papers.map((paper, pi) => (
                    <div key={paper.id} className="row" style={{ justifyContent: 'space-between', gap: 12 }}>
                      <div className="col" style={{ minWidth: 0 }}>
                        <div className="row gap8" style={{ minWidth: 0 }}>
                          <span style={{ fontSize: 13, fontWeight: pi === 0 ? 650 : 400, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                            {paper.title}
                          </span>
                          {pi === 0 && (
                            <span className="pill" style={{ background: 'var(--accent-soft)', color: 'var(--accent-text)', flexShrink: 0 }}>
                              {tr('建议保留', 'Suggested keep')}
                            </span>
                          )}
                        </div>
                        <span className="muted" style={{ fontSize: 12 }}>
                          {paper.year ?? tr('年份未知', 'Year unknown')} · {paper.source ?? tr('来源未知', 'Unknown source')} ·{' '}
                          {tr('全文分段 ', 'Chunks ')}{paper.chunk_count}
                          {paper.has_wiki ? ` · ${tr('已有解读', 'Has wiki')}` : ''}
                        </span>
                      </div>
                      {pi > 0 && (
                        <button
                          className="btn btn-soft sm"
                          disabled={merge.isPending}
                          onClick={() => confirmMerge(keep, paper)}
                          style={{ flexShrink: 0 }}
                        >
                          {tr('并入保留行', 'Merge into keep')}
                        </button>
                      )}
                    </div>
                  ))}
                </div>
              </div>
            );
          })}
        </div>
      )}
    </section>
  );
}

/* —— 本月用量进度 —— */

function BudgetCard({ libraryId }: { libraryId: string }) {
  const { data: budget, isError } = useQuery({
    queryKey: ['library-budget', libraryId],
    queryFn: () => api.getLibraryBudget(libraryId),
    retry: false,
    refetchInterval: 60_000,
  });

  const limited = budget?.monthly_budget != null && budget.monthly_budget > 0;
  const ratio = limited && budget ? Math.min(1, budget.used_tokens / budget.monthly_budget!) : 0;
  const barColor = ratio >= 1 ? 'var(--danger)' : ratio >= 0.8 ? 'var(--warn)' : 'var(--accent)';

  return (
    <section className="card" style={{ padding: 18 }}>
      <div className="row" style={{ justifyContent: 'space-between', marginBottom: 8 }}>
        <h3 style={{ fontSize: 14, fontWeight: 700 }}>{tr('本月 AI 用量', 'AI usage this month')}</h3>
        {budget && <span className="muted" style={{ fontSize: 12 }}>{budget.month}</span>}
      </div>
      {isError ? (
        <div className="muted" style={{ fontSize: 13 }}>{tr('用量加载失败', 'Failed to load usage')}</div>
      ) : !budget ? (
        <div className="skel" style={{ height: 34 }} />
      ) : (
        <div className="col gap8">
          <div className="row" style={{ justifyContent: 'space-between', fontSize: 13 }}>
            <span>
              {tr('已用 ', 'Used ')}
              <strong>{budget.used_tokens.toLocaleString()}</strong>
              {' tokens'}
              <span className="muted" style={{ fontSize: 12, marginLeft: 8 }}>
                {tr('输入 ', 'in ')}{budget.prompt_tokens.toLocaleString()} · {tr('输出 ', 'out ')}
                {budget.completion_tokens.toLocaleString()}
              </span>
            </span>
            <span className="muted" style={{ fontSize: 12 }}>
              {limited
                ? `${tr('上限 ', 'Cap ')}${budget.monthly_budget!.toLocaleString()}`
                : tr('未设上限', 'No cap')}
            </span>
          </div>
          {limited && (
            <div style={{ height: 8, borderRadius: 4, background: 'var(--surface-3)', overflow: 'hidden' }}>
              <div
                style={{
                  width: `${Math.round(ratio * 100)}%`,
                  height: '100%',
                  borderRadius: 4,
                  background: barColor,
                  transition: 'width .3s',
                }}
              />
            </div>
          )}
          {budget.exhausted && (
            <div style={{ color: 'var(--danger-tx)', fontSize: 13 }}>
              {tr(
                '本月预算已用尽：同步任务已暂停，下月自动恢复，或调高上限后再试。',
                'Monthly budget used up: syncing is paused until next month, or raise the cap and retry.',
              )}
            </div>
          )}
        </div>
      )}
    </section>
  );
}

/* —— 库信息与预算 —— */

function LibraryInfoCard({ lib }: { lib: DirectionLibraryDetail }) {
  const queryClient = useQueryClient();
  const [name, setName] = useState(lib.name);
  const [statement, setStatement] = useState(lib.statement ?? '');
  const [cadence, setCadence] = useState(lib.cadence ?? '');
  const [budget, setBudget] = useState(lib.monthly_budget == null ? '' : String(lib.monthly_budget));

  // 库切换 / 保存后回填
  useEffect(() => {
    setName(lib.name);
    setStatement(lib.statement ?? '');
    setCadence(lib.cadence ?? '');
    setBudget(lib.monthly_budget == null ? '' : String(lib.monthly_budget));
  }, [lib]);

  const dirty =
    name !== lib.name ||
    statement !== (lib.statement ?? '') ||
    cadence !== (lib.cadence ?? '') ||
    budget !== (lib.monthly_budget == null ? '' : String(lib.monthly_budget));

  const save = useMutation({
    mutationFn: () =>
      api.updateLibrary(lib.id, {
        name: name.trim() || lib.name,
        statement: statement.trim() || null,
        cadence: cadence || null,
        monthly_budget: budget.trim() === '' ? null : Math.max(0, Math.floor(Number(budget))),
      }),
    onSuccess: () => {
      toast(tr('库信息已保存', 'Library info saved'), 'ok');
      void queryClient.invalidateQueries({ queryKey: ['library', lib.id] });
      void queryClient.invalidateQueries({ queryKey: ['libraries'] });
      void queryClient.invalidateQueries({ queryKey: ['library-budget', lib.id] });
    },
    onError: () => toast(tr('保存失败，请重试', 'Save failed, please retry'), 'error'),
  });

  const budgetInvalid = budget.trim() !== '' && (!Number.isFinite(Number(budget)) || Number(budget) < 0);

  return (
    <section className="card" style={{ padding: 18 }}>
      <div className="row" style={{ justifyContent: 'space-between', marginBottom: 12 }}>
        <h3 style={{ fontSize: 14, fontWeight: 700 }}>{tr('库信息', 'Library info')}</h3>
        <button
          className="btn btn-primary sm"
          disabled={!dirty || budgetInvalid || save.isPending || !name.trim()}
          onClick={() => save.mutate()}
        >
          {save.isPending ? tr('保存中…', 'Saving…') : tr('保存', 'Save')}
        </button>
      </div>
      <div className="col gap12">
        <label className="col gap6">
          <span className="muted" style={{ fontSize: 12 }}>{tr('名称', 'Name')}</span>
          <input className="input" value={name} onChange={(e) => setName(e.target.value)} maxLength={255} />
        </label>
        <label className="col gap6">
          <span className="muted" style={{ fontSize: 12 }}>{tr('方向说明（一句话）', 'Statement')}</span>
          <textarea
            className="input"
            rows={2}
            value={statement}
            onChange={(e) => setStatement(e.target.value)}
            placeholder={tr('这个方向关注什么问题', 'What this direction is about')}
          />
        </label>
        <div className="row gap12" style={{ flexWrap: 'wrap' }}>
          <label className="col gap6" style={{ minWidth: 180 }}>
            <span className="muted" style={{ fontSize: 12 }}>{tr('同步节奏', 'Sync cadence')}</span>
            <select className="input" value={cadence} onChange={(e) => setCadence(e.target.value)}>
              {CADENCES.map((c) => (
                <option key={c.v} value={c.v}>{tr(c.zh, c.en)}</option>
              ))}
            </select>
          </label>
          <label className="col gap6" style={{ minWidth: 220 }}>
            <span className="muted" style={{ fontSize: 12 }}>
              {tr('每月 AI 预算（token 数，留空 = 不限）', 'Monthly AI budget (tokens, empty = unlimited)')}
            </span>
            <input
              className="input"
              inputMode="numeric"
              value={budget}
              onChange={(e) => setBudget(e.target.value)}
              placeholder={tr('如 2000000', 'e.g. 2000000')}
            />
          </label>
        </div>
        {budgetInvalid && (
          <div style={{ color: 'var(--danger-tx)', fontSize: 12 }}>
            {tr('预算需为不小于 0 的数字', 'Budget must be a number ≥ 0')}
          </div>
        )}
      </div>
    </section>
  );
}

/* —— 收录设置（P8：库为收录配置权威源，ingest 按此检索/打分） —— */

const QUICK_CATEGORIES = ['cs.CL', 'cs.AI', 'cs.LG', 'cs.CV', 'cs.MA', 'stat.ML'];

function InclusionSettingsCard({ lib }: { lib: DirectionLibraryDetail }) {
  const queryClient = useQueryClient();
  const def = lib.definition ?? {};
  const [categories, setCategories] = useState<string[]>(def.keywords?.arxiv_categories ?? []);
  const [includeStr, setIncludeStr] = useState((def.keywords?.include ?? []).join(', '));
  const [customCat, setCustomCat] = useState('');
  const [rubric, setRubric] = useState<RubricDimension[]>(def.rubric ?? []);

  useEffect(() => {
    const d = lib.definition ?? {};
    setCategories(d.keywords?.arxiv_categories ?? []);
    setIncludeStr((d.keywords?.include ?? []).join(', '));
    setRubric(d.rubric ?? []);
  }, [lib]);

  const includeTerms = includeStr.split(/[,，\n]/).map((x) => x.trim()).filter(Boolean);

  function toggleCat(c: string) {
    setCategories((prev) => (prev.includes(c) ? prev.filter((x) => x !== c) : [...prev, c]));
  }
  function addCustomCat() {
    const c = customCat.trim();
    if (c && !categories.includes(c)) setCategories((prev) => [...prev, c]);
    setCustomCat('');
  }

  const save = useMutation({
    mutationFn: () =>
      api.updateLibrary(lib.id, {
        keywords: {
          ...(lib.definition?.keywords ?? {}),
          arxiv_categories: categories,
          include: includeTerms,
        },
        rubric: rubric.filter((r) => r.name.trim()),
      }),
    onSuccess: () => {
      toast(tr('收录设置已保存', 'Inclusion settings saved'), 'ok');
      void queryClient.invalidateQueries({ queryKey: ['library', lib.id] });
      void queryClient.invalidateQueries({ queryKey: ['libraries'] });
    },
    onError: () => toast(tr('保存失败，请重试', 'Save failed, please retry'), 'error'),
  });

  return (
    <section className="card" style={{ padding: 18 }}>
      <div className="row" style={{ justifyContent: 'space-between', marginBottom: 4 }}>
        <h3 style={{ fontSize: 14, fontWeight: 700 }}>{tr('收录设置', 'Inclusion settings')}</h3>
        <button className="btn btn-primary sm" disabled={save.isPending} onClick={() => save.mutate()}>
          {save.isPending ? tr('保存中…', 'Saving…') : tr('保存', 'Save')}
        </button>
      </div>
      <p className="muted" style={{ fontSize: 12, marginBottom: 14 }}>
        {tr(
          '文献追踪按这里的 arXiv 分类与关键词检索、按打分标准判定相关性；留空则用默认分类、只按方向说明打分。',
          'Literature tracking searches by these arXiv categories and keywords and scores relevance against the rubric; leave empty to use default categories and statement-only scoring.',
        )}
      </p>

      <div className="col gap6" style={{ marginBottom: 14 }}>
        <span className="muted" style={{ fontSize: 12 }}>{tr('arXiv 分类', 'arXiv categories')}</span>
        <div className="row gap6 wrap">
          {[...new Set([...QUICK_CATEGORIES, ...categories])].map((c) => (
            <button key={c} type="button" className={'chip mono' + (categories.includes(c) ? ' on' : '')} onClick={() => toggleCat(c)}>
              {c}
            </button>
          ))}
        </div>
        <div className="row gap8" style={{ marginTop: 6 }}>
          <input
            className="input"
            style={{ width: 170 }}
            placeholder={tr('自定义分类，如 cs.IR', 'custom, e.g. cs.IR')}
            value={customCat}
            onChange={(e) => setCustomCat(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Enter') { e.preventDefault(); addCustomCat(); } }}
          />
          <button className="btn btn-soft sm" onClick={addCustomCat} disabled={!customCat.trim()}>{tr('添加', 'Add')}</button>
        </div>
      </div>

      <label className="col gap6" style={{ marginBottom: 14 }}>
        <span className="muted" style={{ fontSize: 12 }}>{tr('检索关键词（逗号分隔）', 'Include terms (comma-separated)')}</span>
        <textarea
          className="input"
          rows={2}
          value={includeStr}
          onChange={(e) => setIncludeStr(e.target.value)}
          placeholder={tr('如 agent, tool use, planning', 'e.g. agent, tool use, planning')}
        />
      </label>

      <div className="col gap6">
        <div className="row" style={{ justifyContent: 'space-between' }}>
          <span className="muted" style={{ fontSize: 12 }}>{tr('打分标准（rubric）', 'Scoring rubric')}</span>
          <button className="btn btn-soft sm" onClick={() => setRubric([...rubric, { name: '', description: '', weight: 1 }])}>
            <Icon name="plus" size={12} />
            {tr('添加维度', 'Add dimension')}
          </button>
        </div>
        {rubric.length === 0 ? (
          <div className="muted" style={{ fontSize: 12.5 }}>
            {tr('未设打分维度：只按方向说明判定相关性。', 'No dimensions — relevance is judged by the statement only.')}
          </div>
        ) : (
          <div className="col gap8">
            {rubric.map((r, i) => (
              <div key={i} className="row gap8" style={{ alignItems: 'flex-start' }}>
                <input className="input" style={{ width: 120 }} placeholder={tr('维度名', 'Name')} value={r.name}
                  onChange={(e) => setRubric(rubric.map((x, j) => (j === i ? { ...x, name: e.target.value } : x)))} />
                <input className="input" style={{ flex: 1 }} placeholder={tr('打分标准', 'Criteria')} value={r.description}
                  onChange={(e) => setRubric(rubric.map((x, j) => (j === i ? { ...x, description: e.target.value } : x)))} />
                <input className="input mono" style={{ width: 64 }} inputMode="decimal" value={String(r.weight)}
                  onChange={(e) => setRubric(rubric.map((x, j) => (j === i ? { ...x, weight: Number(e.target.value) || 0 } : x)))} />
                <button className="icon-btn" style={{ height: 38 }} title={tr('删除维度', 'Remove dimension')}
                  onClick={() => setRubric(rubric.filter((_, j) => j !== i))}>
                  <Icon name="trash" size={14} />
                </button>
              </div>
            ))}
          </div>
        )}
      </div>
    </section>
  );
}

/* —— 文献库管理员名单 —— */

function CuratorsCard({ libraryId, canEdit }: { libraryId: string; canEdit: boolean }) {
  const queryClient = useQueryClient();
  const [adding, setAdding] = useState(false);
  const [pick, setPick] = useState('');

  const curatorsQuery = useQuery({
    queryKey: ['library-curators', libraryId],
    queryFn: () => api.listLibraryCurators(libraryId),
    retry: false,
  });
  const usersQuery = useQuery({
    queryKey: ['admin-users'],
    queryFn: () => api.adminListUsers(),
    enabled: canEdit && adding,
    retry: false,
  });

  const curators = curatorsQuery.data ?? [];
  const candidates = useMemo(
    () => (usersQuery.data ?? []).filter((u) => !curators.some((c) => c.user_id === u.id)),
    [usersQuery.data, curators],
  );

  const replace = useMutation({
    mutationFn: (userIds: string[]) => api.setLibraryCurators(libraryId, userIds),
    onSuccess: (rows) => {
      queryClient.setQueryData(['library-curators', libraryId], rows);
      void queryClient.invalidateQueries({ queryKey: ['libraries'] });
      setPick('');
      setAdding(false);
    },
    onError: () => toast(tr('名单更新失败', 'Failed to update the list'), 'error'),
  });

  return (
    <section className="card" style={{ padding: 18 }}>
      <div className="row" style={{ justifyContent: 'space-between', marginBottom: 6 }}>
        <h3 style={{ fontSize: 14, fontWeight: 700 }}>{tr('文献库管理员', 'Library managers')}</h3>
        {canEdit && !adding && (
          <button className="btn btn-soft sm" onClick={() => setAdding(true)}>
            <Icon name="plus" size={12} />
            {tr('添加', 'Add')}
          </button>
        )}
      </div>
      <p className="muted" style={{ fontSize: 12, marginBottom: 12 }}>
        {tr(
          '管理员可以调整这个库的收录标准、编辑库信息与预算、合并重复论文；名单由平台管理员任命。',
          'Managers can tune inclusion criteria, edit library info and budget, and merge duplicate papers. Appointed by platform admins.',
        )}
      </p>
      {curatorsQuery.isError ? (
        <div className="muted" style={{ fontSize: 13 }}>
          {tr('名单加载失败（可能没有查看权限）', 'Failed to load (you may lack permission)')}
        </div>
      ) : curators.length === 0 ? (
        <div className="muted" style={{ fontSize: 13 }}>
          {tr('还没有任命管理员：库背后课题的成员与平台管理员可管理。', 'No managers appointed yet; topic members and platform admins can manage.')}
        </div>
      ) : (
        <div className="col gap8">
          {curators.map((c) => (
            <div key={c.user_id} className="row" style={{ justifyContent: 'space-between' }}>
              <div className="row gap8">
                <Icon name="users" size={14} />
                <span style={{ fontSize: 13 }}>{c.display_name || c.email}</span>
                <span className="muted" style={{ fontSize: 12 }}>{c.email}</span>
              </div>
              {canEdit && (
                <button
                  className="btn btn-ghost sm"
                  disabled={replace.isPending}
                  onClick={() => replace.mutate(curators.filter((x) => x.user_id !== c.user_id).map((x) => x.user_id))}
                >
                  {tr('移除', 'Remove')}
                </button>
              )}
            </div>
          ))}
        </div>
      )}
      {canEdit && adding && (
        <div className="row gap8" style={{ marginTop: 12 }}>
          <select className="input" value={pick} onChange={(e) => setPick(e.target.value)} style={{ flex: 1 }}>
            <option value="">{tr('选择用户…', 'Pick a user…')}</option>
            {candidates.map((u) => (
              <option key={u.id} value={u.id}>
                {u.display_name || u.email}（{u.email}）
              </option>
            ))}
          </select>
          <button
            className="btn btn-primary sm"
            disabled={!pick || replace.isPending}
            onClick={() => replace.mutate([...curators.map((c) => c.user_id), pick])}
          >
            {tr('确认添加', 'Add')}
          </button>
          <button className="btn btn-ghost sm" onClick={() => { setAdding(false); setPick(''); }}>
            {tr('取消', 'Cancel')}
          </button>
        </div>
      )}
    </section>
  );
}
