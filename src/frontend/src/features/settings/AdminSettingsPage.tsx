import { useState } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { EmptyState } from '../../components/ui/EmptyState';
import { PageHead } from '../../components/ui/PageHead';
import { Segmented } from '../../components/ui/Segmented';
import { FeedbackTab } from '../feedback/FeedbackTab';
import { tr } from '../../lib/i18n';
import { api, isAdmin } from '../../lib/api';
import { CodesTab, DailyCategoriesTab, LlmTab, UsageTab, UsersTab } from './SettingsPage';

/* ============================================================
   /admin — 管理员设置：LLM 管理 / 每日论文 / 用户管理 / 注册码 / 反馈 / 用量总览
   各标签页组件仍住在 SettingsPage.tsx（与个人设置共用一批内部小组件），这里只负责壳层。
   ============================================================ */

type AdminTab = 'llm' | 'daily' | 'users' | 'codes' | 'feedback' | 'usage';

const ADMIN_TABS: AdminTab[] = ['llm', 'daily', 'users', 'codes', 'feedback', 'usage'];

export function AdminSettingsPage() {
  const { data: me, isLoading } = useQuery({ queryKey: ['me'], queryFn: () => api.me(), retry: false });
  const admin = isAdmin(me);
  // 支持 /admin?tab=llm 深链
  const [searchParams] = useSearchParams();
  const [tab, setTab] = useState<AdminTab>(() => {
    const t = searchParams.get('tab');
    return t !== null && ADMIN_TABS.includes(t as AdminTab) ? (t as AdminTab) : 'llm';
  });

  const items: { v: AdminTab; label: string }[] = [
    { v: 'llm', label: tr('LLM 管理', 'LLM admin') },
    { v: 'daily', label: tr('每日论文', 'Daily papers') },
    { v: 'users', label: tr('用户管理', 'Users') },
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
          desc={tr('这些设置只对管理员开放，个人设置请到「设置」页。', 'These settings are admin-only. Your own settings live on the Settings page.')}
          action={<Link className="btn btn-soft" to="/settings">{tr('去个人设置', 'Go to settings')}</Link>}
        />
      ) : (
        <>
          <div className="row" style={{ gap: 12, marginBottom: 22, flexWrap: 'wrap', alignItems: 'center' }}>
            <Segmented options={items} value={tab} onChange={setTab} />
          </div>
          {tab === 'llm' && <LlmTab />}
          {tab === 'daily' && <DailyCategoriesTab />}
          {tab === 'users' && <UsersTab />}
          {tab === 'codes' && <CodesTab />}
          {tab === 'feedback' && <FeedbackTab />}
          {tab === 'usage' && <UsageTab />}
        </>
      )}
    </div>
  );
}
