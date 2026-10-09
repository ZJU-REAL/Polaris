/* ============================================================
   设置 → 每日论文：订阅（每人一份，#806）。

   以前是两张卡：「每日新论文订阅分类」（其实只是 arXiv 的分类，占着第一张、写着
   「如 cs.AI」）加一张「其他来源」（PubMed 等塞在这里）。arXiv 被摆成了默认，别的
   领域的人看到的第一件事是计算机的分类。

   现在一张卡、一份清单：每一行是一个来源和它的订阅词，arXiv 只是其中一个来源。能订
   哪些来源由后端按「能不能供每日新增」探测给出，前端不自带名单。整份一起保存——
   后端的 PUT 本来就是整份替换，两张卡各存各的反而要小心别把对方那份清空。
   ============================================================ */
import { useEffect, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Icon } from '../../components/ui/Icon';
import { toast } from '../../components/ui/Toast';
import { api, type DailySubscription } from '../../lib/api';
import { tr } from '../../lib/i18n';
import { SettingsActions, SettingsGroup, SettingsRow, SettingsSection, StatusDot } from './settingsUi';

/** arXiv 分类的大致格式：如 cs.AI / stat.ML / q-bio.NC / hep-th。 */
const ARXIV_CATEGORY_RE = /^[a-z][a-z-]+(\.[A-Za-z]{2,10})?$/;

/** 一个订阅词在这个来源上是否像样；不像样返回给人看的原因。 */
export function termIssue(source: string, term: string): string | null {
  const v = term.trim();
  if (!v) return tr('请输入订阅词', 'Enter a term');
  if (source === 'arxiv' && !ARXIV_CATEGORY_RE.test(v)) {
    return tr('arXiv 需填写分类，例如 cs.AI、q-bio.NC、hep-th', 'arXiv needs a category, e.g. cs.AI, q-bio.NC, hep-th');
  }
  return null;
}

/** 还能添加的来源：后端说能日更、且清单里还没有的。 */
export function addableSources(available: string[], rows: { source: string }[]): string[] {
  return available.filter((s) => !rows.some((r) => r.source === s));
}

/** 保存用的整份订阅。一个词都没有的来源不发——那等于没订。 */
export function subscriptionsPayload(rows: { source: string; terms: string[] }[]) {
  return rows.filter((r) => r.terms.length > 0).map((r) => ({ source: r.source, terms: r.terms }));
}

function termPlaceholder(source: string): string {
  return source === 'arxiv'
    ? tr('例如 cs.AI、q-bio.NC', 'e.g. cs.AI, q-bio.NC')
    : tr('例如 neuroscience', 'e.g. neuroscience');
}

export function DailySubscriptionsSection() {
  const queryClient = useQueryClient();
  const { data, isLoading, isError } = useQuery({
    queryKey: ['daily-subscriptions'],
    queryFn: () => api.getDailySubscriptions(),
    retry: false,
  });
  // 来源的显示名问后端要（与建库表单同一份清单），拿不到就显示 id
  const sourcesQuery = useQuery({
    queryKey: ['literature-sources'],
    queryFn: () => api.listLiteratureSources(),
    retry: false,
  });
  const titleOf = (id: string) => (sourcesQuery.data ?? []).find((s) => s.id === id)?.title ?? id;

  // 本地编辑副本：首次拿到数据后接管，避免 refetch 覆盖未保存的改动
  const [rows, setRows] = useState<DailySubscription[] | null>(null);
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [adding, setAdding] = useState('');
  useEffect(() => {
    if (data && rows === null) setRows(data.subscriptions);
  }, [data, rows]);

  const shown = rows ?? [];
  const addable = addableSources(data?.available_sources ?? [], shown);
  const dirty =
    !!data &&
    JSON.stringify(subscriptionsPayload(shown)) !==
      JSON.stringify(subscriptionsPayload(data.subscriptions));

  const save = useMutation({
    mutationFn: () => api.setDailySubscriptions(subscriptionsPayload(shown)),
    onSuccess: (res) => {
      toast(tr('已保存', 'Saved'), 'ok');
      setRows(res.subscriptions);
      void queryClient.invalidateQueries({ queryKey: ['daily-subscriptions'] });
      void queryClient.invalidateQueries({ queryKey: ['daily-categories'] });
    },
    onError: (e) =>
      toast(`${tr('保存失败', 'Couldn’t save')}：${e instanceof Error ? e.message : String(e)}`, 'error'),
  });

  const addTerm = (source: string) => {
    const v = (drafts[source] ?? '').trim();
    const issue = termIssue(source, v);
    if (issue) {
      toast(issue, 'error');
      return;
    }
    setRows(shown.map((r) => (r.source === source && !r.terms.includes(v) ? { ...r, terms: [...r.terms, v] } : r)));
    setDrafts({ ...drafts, [source]: '' });
  };

  const addSource = (source: string) => {
    if (!source) return;
    setRows([...shown, { source, terms: [], supports_daily: true }]);
    setAdding('');
  };

  const title = tr('订阅', 'Subscriptions');
  const desc = tr('每天从这些来源获取新论文：arXiv 按分类，其他来源按检索词。', 'New papers arrive daily: arXiv by category, other sources by search term.');
  if (isLoading || isError) {
    return (
      <SettingsSection title={title} desc={desc}>
        <SettingsGroup pad>
          <div className="st-row-hint">{isLoading ? tr('加载中…', 'Loading…') : tr('无法加载订阅', 'Couldn’t load subscriptions')}</div>
        </SettingsGroup>
      </SettingsSection>
    );
  }

  return (
    <SettingsSection title={title} desc={desc}>
      <SettingsGroup>
        {shown.length === 0 && (
          <SettingsRow label={tr('还没有订阅', 'No subscriptions yet')} hint={tr('在下方添加来源', 'Add a source below')} />
        )}
        {shown.map((row) => (
          <div key={row.source} className="st-row st-row-stack">
            <div className="row gap8" style={{ alignItems: 'center' }}>
              <span className="st-row-label">{titleOf(row.source)}</span>
              {/* 订了一个供不了日更的源：池子会一直空着而界面上看不出原因 */}
              {!row.supports_daily && (
                <StatusDot tone="warn">{tr('暂时无法每日更新', 'Can’t update daily right now')}</StatusDot>
              )}
              <button
                className="btn btn-ghost sm st-quiet-danger"
                style={{ marginLeft: 'auto' }}
                onClick={() => setRows(shown.filter((r) => r.source !== row.source))}
              >
                {tr('移除', 'Remove')}
              </button>
            </div>
            {row.terms.length > 0 && (
              <div className="row gap6 wrap">
                {row.terms.map((t) => (
                  <span
                    key={t}
                    className={'pill sm' + (row.source === 'arxiv' ? ' mono' : '')}
                    style={{ background: 'var(--surface-3)', gap: 4, paddingRight: 5 }}
                  >
                    {t}
                    <button
                      title={tr('移除', 'Remove')}
                      onClick={() =>
                        setRows(
                          shown.map((r) =>
                            r.source === row.source ? { ...r, terms: r.terms.filter((x) => x !== t) } : r,
                          ),
                        )
                      }
                      style={{ border: 'none', background: 'transparent', cursor: 'pointer', color: 'var(--text-3)', display: 'inline-flex', padding: 1 }}
                    >
                      <Icon name="x" size={10} />
                    </button>
                  </span>
                ))}
              </div>
            )}
            <div className="row gap8">
              <input
                className={'input' + (row.source === 'arxiv' ? ' mono' : '')}
                style={{ width: 240, maxWidth: '100%' }}
                placeholder={termPlaceholder(row.source)}
                value={drafts[row.source] ?? ''}
                onChange={(e) => setDrafts({ ...drafts, [row.source]: e.target.value })}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' && !e.nativeEvent.isComposing) {
                    e.preventDefault();
                    addTerm(row.source);
                  }
                }}
              />
              <button className="btn btn-ghost sm" disabled={!(drafts[row.source] ?? '').trim()} onClick={() => addTerm(row.source)}>
                <Icon name="plus" size={12} />
                {tr('添加', 'Add')}
              </button>
            </div>
            {row.terms.length === 0 && (
              <div className="st-row-hint">
                {tr('添加至少一个订阅词后才会生效', 'Add at least one term to subscribe')}
              </div>
            )}
          </div>
        ))}
      </SettingsGroup>

      <SettingsActions
        note={addable.length > 0 ? (
          <select
            className="input"
            style={{ width: 200 }}
            value={adding}
            onChange={(e) => addSource(e.target.value)}
          >
            <option value="">{tr('添加来源…', 'Add a source…')}</option>
            {addable.map((s) => (
              <option key={s} value={s}>
                {titleOf(s)}
              </option>
            ))}
          </select>
        ) : tr('所有可用来源都已添加', 'All available sources are added')}
      >
        <button className="btn btn-primary sm" disabled={!dirty || save.isPending} onClick={() => save.mutate()}>
          {save.isPending ? tr('保存中…', 'Saving…') : tr('保存', 'Save')}
        </button>
      </SettingsActions>
    </SettingsSection>
  );
}
