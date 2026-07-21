import { useEffect, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Icon } from '../../components/ui/Icon';
import { FormField } from '../../components/ui/FormField';
import { toast } from '../../components/ui/Toast';
import { api } from '../../lib/api';
import { fmtRelative } from '../../lib/format';
import { tr } from '../../lib/i18n';
import { AuthorBindWizard, errText } from '../library/AuthorBindWizard';

/* ============================================================
   设置页「学术身份」分区：管理作者绑定
   （姓名变体 + 机构 + OpenAlex 作者实体 + 自动同步开关）。
   绑定向导与 Library 的「我发表的」tab 共用 AuthorBindWizard；
   这里选中实体只保存绑定，不强制触发同步（syncOnLink=false）。
   ============================================================ */

/** 姓名变体输入：逗号或换行分隔 → 去空白去重。 */
function parseNames(raw: string): string[] {
  return [...new Set(raw.split(/[\n,，]/).map((s) => s.trim()).filter(Boolean))];
}

/** 机构输入：每行一个（机构名里可能含逗号，不按逗号切）。 */
function parseAffiliations(raw: string): string[] {
  return [...new Set(raw.split(/\n/).map((s) => s.trim()).filter(Boolean))];
}

export function AcademicIdentitySection() {
  const queryClient = useQueryClient();
  const profileQuery = useQuery({
    queryKey: ['author-profile'],
    queryFn: () => api.getAuthorProfile(),
    retry: false,
  });
  const profile = profileQuery.data ?? null;

  // 未绑定时的「去绑定」内联向导 / 已绑定时的「重新选择作者实体」内联向导
  const [wizardOpen, setWizardOpen] = useState(false);

  // 已绑定时的可编辑草稿（profile 变化时重新填充）
  const [namesRaw, setNamesRaw] = useState('');
  const [affsRaw, setAffsRaw] = useState('');
  const [autoSync, setAutoSync] = useState(true);
  useEffect(() => {
    if (profileQuery.data) {
      setNamesRaw(profileQuery.data.name_variants.join('\n'));
      setAffsRaw(profileQuery.data.affiliations.join('\n'));
      setAutoSync(profileQuery.data.auto_sync);
    }
  }, [profileQuery.data]);

  const saveMutation = useMutation({
    mutationFn: () =>
      api.saveAuthorProfile({
        name_variants: parseNames(namesRaw),
        affiliations: parseAffiliations(affsRaw),
        openalex_author_id: profile?.openalex_author_id ?? null,
        orcid: profile?.orcid ?? null,
        auto_sync: autoSync,
      }),
    onSuccess: (saved) => {
      queryClient.setQueryData(['author-profile'], saved);
      void queryClient.invalidateQueries({ queryKey: ['author-profile'] });
      void queryClient.invalidateQueries({ queryKey: ['publications'] });
      toast(tr('学术身份已保存', 'Academic identity saved'), 'ok');
    },
    onError: (e) => toast(`${tr('保存失败：', 'Save failed: ')}${errText(e)}`, 'error'),
  });

  const dirty =
    profile !== null &&
    (namesRaw !== profile.name_variants.join('\n') ||
      affsRaw !== profile.affiliations.join('\n') ||
      autoSync !== profile.auto_sync);

  return (
    <div className="card card-pad" style={{ maxWidth: 560, marginTop: 20 }}>
      <div className="section-h" style={{ marginBottom: 4 }}>
        <Icon name="users" size={15} style={{ color: 'var(--accent)' }} />
        {tr('学术身份', 'Academic identity')}
      </div>
      <div style={{ fontSize: 12.5, color: 'var(--text-3)', marginBottom: 14, lineHeight: 1.5 }}>
        {tr(
          '绑定你的作者姓名和机构，用于自动收集你发表的论文；论文列表在「文献库 → 我发表的」里查看。',
          'Bind your author name and affiliation so we can collect your publications; see them under Library → My publications.',
        )}
      </div>

      {profileQuery.isLoading ? (
        <div className="empty" style={{ padding: 20 }}>{tr('加载中…', 'Loading…')}</div>
      ) : profileQuery.isError ? (
        <div className="empty" style={{ padding: 20 }}>
          {tr('绑定信息暂时加载不出来（后端不可用）', 'Failed to load your author info (backend unavailable)')}
          <div style={{ marginTop: 10 }}>
            <button className="btn btn-soft sm" onClick={() => void profileQuery.refetch()}>
              {tr('重试', 'Retry')}
            </button>
          </div>
        </div>
      ) : profile === null ? (
        /* —— 未绑定 —— */
        <>
          <div className="row gap12" style={{ alignItems: 'center' }}>
            <span style={{ fontSize: 12.5, color: 'var(--text-2)', flex: 1 }}>
              {tr('还没绑定作者信息。', 'No author info bound yet.')}
            </span>
            {!wizardOpen && (
              <button className="btn btn-primary sm" onClick={() => setWizardOpen(true)}>
                <Icon name="plus" size={13} />
                {tr('去绑定', 'Bind now')}
              </button>
            )}
          </div>
          {wizardOpen && (
            <AuthorBindWizard
              profile={null}
              embedded
              syncOnLink={false}
              onDone={() => setWizardOpen(false)}
              onCancel={() => setWizardOpen(false)}
            />
          )}
        </>
      ) : (
        /* —— 已绑定：可编辑表单 —— */
        <>
          <FormField
            label="姓名变体"
            en="Name variants"
            hint={tr('论文署名可能用到的所有写法，逗号或换行分隔', 'All spellings you publish under, comma or newline separated')}
          >
            <textarea
              className="textarea"
              rows={2}
              value={namesRaw}
              onChange={(e) => setNamesRaw(e.target.value)}
              placeholder={'San Zhang, Zhang San'}
              style={{ width: '100%', fontSize: 12.5 }}
            />
          </FormField>
          <FormField
            label="机构"
            en="Affiliations"
            hint={tr('每行一个机构名', 'One affiliation per line')}
            style={{ marginTop: 12 }}
          >
            <textarea
              className="textarea"
              rows={2}
              value={affsRaw}
              onChange={(e) => setAffsRaw(e.target.value)}
              placeholder={'Zhejiang University'}
              style={{ width: '100%', fontSize: 12.5 }}
            />
          </FormField>

          {/* —— 已关联的作者实体 —— */}
          <div
            className="card"
            style={{ padding: '10px 14px', marginTop: 14, display: 'flex', gap: 12, alignItems: 'center', flexWrap: 'wrap' }}
          >
            <div style={{ flex: 1, minWidth: 200 }}>
              {profile.openalex_author_id ? (
                <>
                  <div style={{ fontSize: 12.5, fontWeight: 650 }}>
                    {tr('已关联作者实体', 'Author entry linked')}
                    <span style={{ color: 'var(--text-3)', fontWeight: 500 }}>
                      {` · ${profile.name_variants[0] ?? '—'}`}
                    </span>
                  </div>
                  <div className="mono" style={{ fontSize: 10.5, color: 'var(--text-4)', marginTop: 3 }}>
                    {profile.openalex_author_id}
                    {profile.orcid && ` · ${profile.orcid}`}
                  </div>
                </>
              ) : (
                <>
                  <div style={{ fontSize: 12.5, fontWeight: 650, color: 'var(--text-2)' }}>
                    {tr('未关联作者实体', 'No author entry linked')}
                  </div>
                  <div style={{ fontSize: 11, color: 'var(--text-4)', marginTop: 3 }}>
                    {tr('关联后才能自动同步你发表的论文', 'Link one to enable automatic publication sync')}
                  </div>
                </>
              )}
              <div className="mono" style={{ fontSize: 10.5, color: 'var(--text-4)', marginTop: 3 }}>
                {profile.last_synced_at
                  ? tr(`上次同步：${fmtRelative(profile.last_synced_at)}`, `Last synced: ${fmtRelative(profile.last_synced_at)}`)
                  : tr('尚未同步', 'Not synced yet')}
              </div>
            </div>
            <button className="btn btn-soft sm" style={{ flexShrink: 0 }} onClick={() => setWizardOpen((v) => !v)}>
              <Icon name="search" size={13} />
              {wizardOpen
                ? tr('收起', 'Collapse')
                : profile.openalex_author_id
                  ? tr('重新选择作者实体', 'Re-pick author entry')
                  : tr('选择作者实体', 'Pick author entry')}
            </button>
          </div>
          {wizardOpen && (
            <AuthorBindWizard
              profile={profile}
              embedded
              syncOnLink={false}
              onDone={() => setWizardOpen(false)}
              onCancel={() => setWizardOpen(false)}
            />
          )}

          {/* —— 自动同步 + 保存 —— */}
          <label className="row" style={{ gap: 10, cursor: 'pointer', alignItems: 'flex-start', marginTop: 14 }}>
            <input
              type="checkbox"
              checked={autoSync}
              onChange={(e) => setAutoSync(e.target.checked)}
              style={{ marginTop: 2 }}
            />
            <span>
              <span style={{ fontWeight: 600 }}>{tr('自动同步', 'Auto sync')}</span>
              <span style={{ display: 'block', fontSize: 12.5, color: 'var(--text-3)', marginTop: 2 }}>
                {tr(
                  '定期从 OpenAlex 拉取你名下的新论文，进「我发表的」待确认列表。',
                  'Periodically fetches new papers under your name from OpenAlex into the to-confirm list.',
                )}
              </span>
            </span>
          </label>
          <div className="row" style={{ justifyContent: 'flex-end', marginTop: 14 }}>
            <button
              className="btn btn-primary"
              disabled={saveMutation.isPending || !dirty || parseNames(namesRaw).length === 0}
              onClick={() => saveMutation.mutate()}
            >
              {saveMutation.isPending ? tr('保存中…', 'Saving…') : tr('保存', 'Save')}
            </button>
          </div>
        </>
      )}
    </div>
  );
}
