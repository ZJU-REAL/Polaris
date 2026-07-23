import { useEffect, useMemo, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Icon } from '../../components/ui/Icon';
import { toast } from '../../components/ui/Toast';
import { api, isAdmin, type DirectionLibraryDetail } from '../../lib/api';
import { tr } from '../../lib/i18n';

/* ============================================================
   文献库「治理」页签（P6）：
   - 库信息与预算编辑（可管理者：成员 / 文献库管理员 / 平台管理员）；
   - 文献库管理员名单（仅平台管理员可编辑）；
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
      <CuratorsCard libraryId={libraryId} canEdit={admin} />
    </div>
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
