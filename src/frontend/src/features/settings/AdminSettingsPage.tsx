import { useState } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { EmptyState } from '../../components/ui/EmptyState';
import { PageHead } from '../../components/ui/PageHead';
import { Segmented } from '../../components/ui/Segmented';
import { ExperimentSettings } from './ExperimentSettings';
import { FeedbackTab } from '../feedback/FeedbackTab';
import { tr } from '../../lib/i18n';
import { api, isAdmin } from '../../lib/api';
import { CodesTab, DailyCategoriesTab, LlmTab, UsageTab, UsersTab } from './SettingsPage';

/* ============================================================
   /admin — 管理员设置：LLM 管理 / 每日论文 / 用户管理 / 注册码 / 反馈 / 用量总览
   各标签页组件仍住在 SettingsPage.tsx（与个人设置共用一批内部小组件），这里只负责壳层。
   ============================================================ */

type AdminTab = 'llm' | 'experiment' | 'daily' | 'users' | 'codes' | 'feedback' | 'usage';

const ADMIN_TABS: AdminTab[] = ['llm', 'experiment', 'daily', 'users', 'codes', 'feedback', 'usage'];

export function AdminSettingsPage() {
  const { data: me, isLoading } = useQuery({ queryKey: ['me'], queryFn: () => api.me(), retry: false });
  const admin = isAdmin(me);
  // 只读账号（游客）看不到成员名册：那一屏是真实的人的邮箱、用户名和用量。
  // 后端也会拒（READ_ONLY_NO_PERSONAL_DATA）——这里只是别让入口摆在那里点了报错。
  const canSeeRoster = !me?.read_only;
  // 支持 /admin?tab=llm 深链
  const [searchParams] = useSearchParams();
  const [tab, setTab] = useState<AdminTab>(() => {
    const t = searchParams.get('tab');
    return t !== null && ADMIN_TABS.includes(t as AdminTab) ? (t as AdminTab) : 'llm';
  });

  // 深链 /admin?tab=users 进来的游客回落到第一个标签，而不是停在一片空白上
  // （me 还没回来时初值已经定成 users 了，所以这里按渲染时的实际权限再判一次）
  const shownTab: AdminTab = tab === 'users' && !canSeeRoster ? 'llm' : tab;

  const items: { v: AdminTab; label: string }[] = [
    { v: 'llm', label: tr('LLM 管理', 'LLM admin') },
    { v: 'experiment', label: tr('实验设置', 'Experiments') },
    { v: 'daily', label: tr('每日论文', 'Daily papers') },
    ...(canSeeRoster ? [{ v: 'users' as const, label: tr('用户管理', 'Users') }] : []),
    { v: 'codes', label: tr('注册码', 'Codes') },
    { v: 'feedback', label: tr('反馈', 'Feedback') },
    { v: 'usage', label: tr('用量总览', 'Usage overview') },
  ];

  return (
    <div className="page fadeup">
      <PageHead eyebrow="Polaris · Manage" title={tr('管理', 'Manage')} />
      {isLoading ? (
        <div className="empty">{tr('加载中…', 'Loading…')}</div>
      ) : !admin ? (
        <EmptyState
          icon="shield"
          title={tr('无权访问', 'No access')}
          desc={tr('这些设置只对管理员开放。', 'These settings are admin-only.')}
          action={<Link className="btn btn-soft" to="/settings">{tr('去个人设置', 'Go to settings')}</Link>}
        />
      ) : (
        <>
          <div className="row" style={{ gap: 12, marginBottom: 22, flexWrap: 'wrap', alignItems: 'center' }}>
            <Segmented options={items} value={shownTab} onChange={setTab} />
          </div>
          {shownTab === 'llm' && <LlmTab />}
          {shownTab === 'experiment' && <ExperimentSettings />}
          {shownTab === 'daily' && <DailyCategoriesTab />}
          {shownTab === 'users' && canSeeRoster && <UsersTab />}
          {shownTab === 'codes' && <CodesTab />}
          {shownTab === 'feedback' && <FeedbackTab />}
          {shownTab === 'usage' && <UsageTab />}
        </>
      )}
    </div>
  );
}
