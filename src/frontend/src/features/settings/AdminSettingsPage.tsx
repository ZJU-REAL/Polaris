import { useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { PageHead } from '../../components/ui/PageHead';
import { Segmented } from '../../components/ui/Segmented';
import { ExperimentSettings } from './ExperimentSettings';
import { LiteratureSearchSettingsPanel } from './LiteratureSearchSettings';
import { DocumentProcessingSettingsPanel } from './DocumentProcessingSettings';
import { tr } from '../../lib/i18n';
import { api } from '../../lib/api';
import { DailyCategoriesTab, LlmTab, UsageTab } from './SettingsPage';

/* ============================================================
   /admin — 管理员设置：LLM 管理 / 每日论文 / 用量总览
   各标签页组件仍住在 SettingsPage.tsx（与个人设置共用一批内部小组件），这里只负责壳层。
   ============================================================ */

type AdminTab = 'llm' | 'literature' | 'processing' | 'experiment' | 'daily' | 'usage';

const ADMIN_TABS: AdminTab[] = ['llm', 'literature', 'processing', 'experiment', 'daily', 'usage'];

export function AdminSettingsPage() {
  // 管理页对所有登录用户开放（role 治理已随 #614 移除）；me 仅用于确认已登录
  const { isLoading } = useQuery({ queryKey: ['me'], queryFn: () => api.me(), retry: false });
  // 支持 /admin?tab=llm 深链
  const [searchParams] = useSearchParams();
  const [tab, setTab] = useState<AdminTab>(() => {
    const t = searchParams.get('tab');
    return t !== null && ADMIN_TABS.includes(t as AdminTab) ? (t as AdminTab) : 'llm';
  });

  const items: { v: AdminTab; label: string }[] = [
    { v: 'llm', label: tr('LLM 管理', 'LLM admin') },
    { v: 'literature', label: tr('文献检索', 'Literature search') },
    { v: 'processing', label: tr('文档处理', 'Document processing') },
    { v: 'experiment', label: tr('实验设置', 'Experiments') },
    { v: 'daily', label: tr('每日论文', 'Daily papers') },
    { v: 'usage', label: tr('用量总览', 'Usage overview') },
  ];

  return (
    <div className="page fadeup">
      <PageHead eyebrow="Polaris · Manage" title={tr('管理', 'Manage')} />
      {isLoading ? (
        <div className="empty">{tr('加载中…', 'Loading…')}</div>
      ) : (
        <>
          <div className="row" style={{ gap: 12, marginBottom: 22, flexWrap: 'wrap', alignItems: 'center' }}>
            <Segmented options={items} value={tab} onChange={setTab} />
          </div>
          {tab === 'llm' && <LlmTab />}
          {tab === 'literature' && <LiteratureSearchSettingsPanel />}
          {tab === 'processing' && <DocumentProcessingSettingsPanel />}
          {tab === 'experiment' && <ExperimentSettings />}
          {tab === 'daily' && <DailyCategoriesTab />}
          {tab === 'usage' && <UsageTab />}
        </>
      )}
    </div>
  );
}
