import { useEffect, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Switch } from '../../components/ui/Switch';
import { toast } from '../../components/ui/Toast';
import { api } from '../../lib/api';
import { fmtRelative } from '../../lib/format';
import { tr } from '../../lib/i18n';
import { errText, MultiValueInput } from '../library/AuthorBindWizard';
import { SettingsActions, SettingsGroup, SettingsRow, SettingsSection } from './settingsUi';

/* ============================================================
   设置页学术身份分区：纯表单管理署名信息
   （多个姓名写法 + 多个机构 + 每日自动匹配开关）。
   保存后每天自动从文献库匹配你发表的论文，
   列表在文献库 → 我发表的里查看。
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
      toast(tr('已保存', 'Saved'), 'ok');
    },
    onError: (e) => toast(`${tr('保存失败：', 'Couldn’t save: ')}${errText(e)}`, 'error'),
  });

  const dirty =
    profile === null
      ? names.length > 0 || affiliations.length > 0
      : names.join('\n') !== profile.name_variants.join('\n') ||
        affiliations.join('\n') !== profile.affiliations.join('\n') ||
        autoSync !== profile.auto_sync;

  return (
    <SettingsSection title={tr('学术身份', 'Academic identity')} desc={tr('用来找出你发表的论文。', 'Used to find papers you’ve published.')}>
      {profileQuery.isLoading ? (
        <SettingsGroup pad>
          <div className="st-row-hint">{tr('加载中…', 'Loading…')}</div>
        </SettingsGroup>
      ) : profileQuery.isError ? (
        <SettingsGroup>
          <SettingsRow label={tr('无法加载学术身份', 'Couldn’t load your academic identity')}>
            <button className="btn btn-ghost sm" onClick={() => void profileQuery.refetch()}>
              {tr('重试', 'Retry')}
            </button>
          </SettingsRow>
        </SettingsGroup>
      ) : (
        <>
          <SettingsGroup>
            <SettingsRow stack label={tr('姓名写法', 'Name variants')} hint={tr('论文署名用过的写法，按回车添加', 'Spellings you publish under. Press Enter to add.')}>
              <MultiValueInput values={names} onChange={setNames} placeholder={tr('例如 San Zhang', 'e.g. Jane Smith')} />
            </SettingsRow>
            <SettingsRow stack label={tr('机构', 'Affiliations')} hint={tr('按回车添加', 'Press Enter to add')}>
              <MultiValueInput values={affiliations} onChange={setAffiliations} placeholder={tr('例如 浙江大学', 'e.g. Zhejiang University')} />
            </SettingsRow>
            <SettingsRow
              labelId="academic-auto-sync"
              label={tr('每天查找我发表的论文', 'Find my papers daily')}
              hint={profile?.last_synced_at
                ? tr(`找到的论文放进「我发表的」，上次查找 ${fmtRelative(profile.last_synced_at)}`, `Matches go to My publications. Last run ${fmtRelative(profile.last_synced_at)}.`)
                : tr('找到的论文放进「我发表的」，由你确认', 'Matches go to My publications for you to confirm')}
            >
              <Switch checked={autoSync} onChange={setAutoSync} aria-labelledby="academic-auto-sync" />
            </SettingsRow>
          </SettingsGroup>
          <SettingsActions>
            <button
              className="btn btn-primary sm"
              disabled={saveMutation.isPending || !dirty || names.length === 0}
              onClick={() => saveMutation.mutate()}
            >
              {saveMutation.isPending ? tr('保存中…', 'Saving…') : tr('保存', 'Save')}
            </button>
          </SettingsActions>
        </>
      )}
    </SettingsSection>
  );
}
