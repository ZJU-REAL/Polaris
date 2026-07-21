import { useEffect, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Icon } from '../../components/ui/Icon';
import { FormField } from '../../components/ui/FormField';
import { Switch } from '../../components/ui/Switch';
import { toast } from '../../components/ui/Toast';
import { api } from '../../lib/api';
import { fmtRelative } from '../../lib/format';
import { tr } from '../../lib/i18n';
import { errText, MultiValueInput } from '../library/AuthorBindWizard';

/* ============================================================
   设置页「学术身份」分区：纯表单管理署名信息
   （多个姓名写法 + 多个机构 + 每日自动匹配开关）。
   保存后每天自动从文献库匹配你发表的论文，
   列表在「文献库 → 我发表的」里查看。
   ============================================================ */

export function AcademicIdentitySection() {
  const queryClient = useQueryClient();
  const profileQuery = useQuery({
    queryKey: ['author-profile'],
    queryFn: () => api.getAuthorProfile(),
    retry: false,
  });
  const profile = profileQuery.data ?? null;

  // 可编辑草稿（profile 变化时重新填充；未绑定时保持空表单）
  const [names, setNames] = useState<string[]>([]);
  const [affiliations, setAffiliations] = useState<string[]>([]);
  const [autoSync, setAutoSync] = useState(true);
  useEffect(() => {
    if (profileQuery.data) {
      setNames(profileQuery.data.name_variants);
      setAffiliations(profileQuery.data.affiliations);
      setAutoSync(profileQuery.data.auto_sync);
    }
  }, [profileQuery.data]);

  const saveMutation = useMutation({
    mutationFn: () =>
      api.saveAuthorProfile({
        name_variants: names,
        affiliations,
        openalex_author_id: null,
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
    profile === null
      ? names.length > 0 || affiliations.length > 0
      : names.join('\n') !== profile.name_variants.join('\n') ||
        affiliations.join('\n') !== profile.affiliations.join('\n') ||
        autoSync !== profile.auto_sync;

  return (
    <div className="card card-pad" style={{ maxWidth: 560, marginTop: 20 }}>
      <div className="section-h" style={{ marginBottom: 4 }}>
        <Icon name="users" size={15} style={{ color: 'var(--accent)' }} />
        {tr('学术身份', 'Academic identity')}
      </div>
      <div style={{ fontSize: 12.5, color: 'var(--text-3)', marginBottom: 14, lineHeight: 1.5 }}>
        {tr(
          '填写论文署名用的姓名和机构，用来从文献库匹配你发表的论文；论文列表在「文献库 → 我发表的」里查看。',
          'List the names and affiliations you publish under so we can match your publications from the library; see them under Library → My publications.',
        )}
      </div>

      {profileQuery.isLoading ? (
        <div className="empty" style={{ padding: 20 }}>{tr('加载中…', 'Loading…')}</div>
      ) : profileQuery.isError ? (
        <div className="empty" style={{ padding: 20 }}>
          {tr('署名信息暂时加载不出来（后端不可用）', 'Failed to load your author info (backend unavailable)')}
          <div style={{ marginTop: 10 }}>
            <button className="btn btn-soft sm" onClick={() => void profileQuery.refetch()}>
              {tr('重试', 'Retry')}
            </button>
          </div>
        </div>
      ) : (
        <>
          <FormField
            label="姓名写法"
            en="Name variants"
            hint={tr('论文署名可能用到的所有写法，回车添加', 'All spellings you publish under — Enter to add')}
          >
            <MultiValueInput values={names} onChange={setNames} placeholder="e.g. San Zhang" />
          </FormField>
          <FormField
            label="机构"
            en="Affiliations"
            hint={tr('回车添加，可以填多个', 'Enter to add; multiple allowed')}
            style={{ marginTop: 12 }}
          >
            <MultiValueInput values={affiliations} onChange={setAffiliations} placeholder="e.g. Zhejiang University" />
          </FormField>

          {profile?.last_synced_at && (
            <div className="mono" style={{ fontSize: 10.5, color: 'var(--text-4)', marginTop: 10 }}>
              {tr(`上次匹配：${fmtRelative(profile.last_synced_at)}`, `Last matched: ${fmtRelative(profile.last_synced_at)}`)}
            </div>
          )}

          {/* —— 每日自动匹配 + 保存 —— */}
          <div className="row" style={{ gap: 16, alignItems: 'center', marginTop: 14 }}>
            <div style={{ flex: 1, minWidth: 0 }}>
              <div id="academic-auto-sync" style={{ fontSize: 13, fontWeight: 600, lineHeight: 1.4 }}>
                {tr('每天自动从文献库匹配', 'Match from the library daily')}
              </div>
              <div style={{ fontSize: 12, color: 'var(--text-3)', lineHeight: 1.45, marginTop: 2 }}>
                {tr(
                  '命中的论文会进「我发表的」待确认列表，由你确认是不是你的。',
                  'Matched papers land in the to-confirm list under My publications for you to review.',
                )}
              </div>
            </div>
            <Switch checked={autoSync} onChange={setAutoSync} aria-labelledby="academic-auto-sync" />
          </div>
          <div className="row" style={{ justifyContent: 'flex-end', marginTop: 14 }}>
            <button
              className="btn btn-primary"
              disabled={saveMutation.isPending || !dirty || names.length === 0}
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
