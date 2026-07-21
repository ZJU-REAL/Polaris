import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Icon } from '../../components/ui/Icon';
import { FormField } from '../../components/ui/FormField';
import { toast } from '../../components/ui/Toast';
import { api, type AuthorCandidate, type AuthorProfile } from '../../lib/api';
import { tr } from '../../lib/i18n';

/* ============================================================
   作者绑定向导（PublicationsTab 与设置页「学术身份」共用）：
   姓名+机构 → OpenAlex 候选实体 → 这是我 / 都不是跳过
   ============================================================ */

export function errText(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
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

export function AuthorBindWizard({
  profile,
  onDone,
  onCancel,
  syncOnLink = true,
  embedded = false,
}: {
  /** 已有绑定时用于预填（修改绑定入口）；未绑定时为 null。 */
  profile: AuthorProfile | null;
  onDone: () => void;
  onCancel?: () => void;
  /** 关联到作者实体后是否立即触发一次发表同步（「我发表的」tab 用 true，设置页只保存不同步）。 */
  syncOnLink?: boolean;
  /** 内嵌进别的卡片时不再套自己的卡片外壳。 */
  embedded?: boolean;
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
      if (syncOnLink && saved.openalex_author_id) {
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

  const body = (
    <>
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
    </>
  );

  if (embedded) {
    return (
      <div style={{ marginTop: 16, paddingTop: 16, borderTop: '1px solid var(--border)' }}>{body}</div>
    );
  }
  return (
    <div className="card card-pad" style={{ maxWidth: 680 }}>
      {body}
    </div>
  );
}
