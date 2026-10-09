import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useQueryClient } from '@tanstack/react-query';
import { Icon, type IconName } from '../../components/ui/Icon';
import { toast } from '../../components/ui/Toast';
import { topicPath, useProject } from '../../app/project';
import { api } from '../../lib/api';
import { fmtTime } from '../../lib/format';
import { getLang, tr } from '../../lib/i18n';
import { useLibraries } from '../libraries/hooks';
import { SettingsGroup } from '../settings/settingsUi';

/* ============================================================
   /start — 落地页：选择或创建课题 + 不依赖课题的功能入口。
   没有任何课题时，课题作用域路由（RequireTopic）统一重定向到这里；
   已有课题的用户手动访问也能在此快速切换。
   下方卡片区对所有人常驻：平台有一半功能（文献库、每日新论文、
   实验室工作台、我的文献库）不需要课题，新用户先逛这些也行。
   ============================================================ */

/**
 * 不依赖课题的功能入口。
 * 注意：模块级常量在 import 时求值，不能用 tr()（切语言不会更新），
 * 所以中英两份原文都存下来，渲染时按当前语言取。
 */
const ENTRIES: { to: string; icon: IconName; zh: [string, string]; en: [string, string] }[] = [
  {
    to: '/libraries',
    icon: 'book',
    zh: ['文献库', '按研究方向收集论文，浏览、搜索、提问。'],
    en: ['Libraries', 'Papers collected by research direction. Browse, search and ask.'],
  },
  {
    to: '/daily',
    icon: 'heart',
    zh: ['每日新论文', '每天从你订阅的来源获取新论文。'],
    en: ['Daily papers', 'New papers every day from your subscriptions.'],
  },
  {
    to: '/lab',
    icon: 'compass',
    zh: ['文献任务', '查看文献库建立、同步和每日论文的任务。'],
    en: ['Library tasks', 'Library builds, syncs and daily paper fetches.'],
  },
  {
    to: '/library',
    icon: 'bookmark',
    zh: ['我的文献库', '你自己收藏的论文。'],
    en: ['My library', 'Papers you’ve saved.'],
  },
];

/** 一键创建的示例课题预设（同上：中英两份原文，调用处再选）。 */
const SAMPLE_TOPIC = {
  zh: {
    name: '示例：递归自我改进 (RSI)',
    statement: '研究模型如何自我改进的能力边界与安全性。',
  },
  en: {
    name: 'Sample: Recursive Self-Improvement (RSI)',
    statement: 'How far models can improve themselves, and how to keep it safe.',
  },
};
/** 判重用：两种语言的名字都算「已经有示例课题了」。 */
const SAMPLE_NAMES = [SAMPLE_TOPIC.zh.name, SAMPLE_TOPIC.en.name];

/** 示例课题绑定的文献库（按名字匹配；没有就不绑）。 */
const SAMPLE_LIBRARY_NAMES = ['Recursive Self-Improvement', 'RSI'];

/** 示例课题的相关研究里预置的三篇论文（arXiv id）。 */
const SAMPLE_PAPER_IDS = ['2506.10943', '2505.03335', '2203.14465'];

export function StartPage() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { projects, isLoading, currentProjectId, setCurrentProjectId } = useProject();
  const [creatingSample, setCreatingSample] = useState(false);

  // 示例课题的语料：有 RSI 方向的文献库就绑上；没有就空着建（允许无语料）
  const librariesQuery = useLibraries();

  function openTopic(id: string) {
    setCurrentProjectId(id);
    navigate(topicPath(id));
  }

  /** 一键创建示例课题；已经有同名的就直接进去，不重复建。 */
  async function createSample() {
    const existing = projects.find((p) => SAMPLE_NAMES.includes(p.name));
    if (existing) {
      toast(tr('已有示例课题，正在打开', 'Opening your sample topic'), 'info');
      openTopic(existing.id);
      return;
    }
    const preset = getLang() === 'en' ? SAMPLE_TOPIC.en : SAMPLE_TOPIC.zh;
    // 语料绑 RSI 文献库（示例课题就是讲这个方向）
    const lib = (librariesQuery.data ?? []).find((l) =>
      SAMPLE_LIBRARY_NAMES.some((n) => l.name.toLowerCase().includes(n.toLowerCase())),
    );
    setCreatingSample(true);
    try {
      const created = await api.createProject({
        name: preset.name,
        statement: preset.statement,
        source_library_ids: lib ? [lib.id] : [],
      });
      // 预置几篇代表作进「相关研究」：逐篇幂等导入，单篇失败不影响建课题
      const seeded = await Promise.allSettled(
        SAMPLE_PAPER_IDS.map((arxivId) => api.importToShelf(created.id, { arxiv_id: arxivId })),
      );
      const okCount = seeded.filter((r) => r.status === 'fulfilled').length;
      await queryClient.invalidateQueries({ queryKey: ['projects'] });
      toast(
        `${tr('已创建示例课题', 'Sample topic created')}${
          lib ? `${tr('，文献库：', '. Library: ')}${lib.name}` : ''
        }${okCount > 0 ? tr(`，${okCount} 篇论文`, `, ${okCount} papers`) : ''}`,
        'ok',
      );
      openTopic(created.id);
    } catch (e) {
      toast(`${tr('创建失败：', 'Couldn’t create: ')}${e instanceof Error ? e.message : String(e)}`, 'error');
    } finally {
      setCreatingSample(false);
    }
  }

  return (
    <div className="page fadeup" style={{ maxWidth: 640, margin: '0 auto', paddingTop: 48 }}>
      <div style={{ textAlign: 'center', marginBottom: 28 }}>
        <div
          style={{
            width: 52,
            height: 52,
            borderRadius: 14,
            margin: '0 auto 18px',
            background: 'var(--accent-soft)',
            color: 'var(--accent)',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
          }}
        >
          <Icon name="layers" size={24} />
        </div>
        <h1 style={{ fontSize: 20, fontWeight: 600, margin: 0 }}>
          {tr('选择或创建课题', 'Pick or create a topic')}
        </h1>
      </div>

      {isLoading ? (
        <div className="col gap10">
          <div className="skel" style={{ width: '100%', height: 56 }} />
          <div className="skel" style={{ width: '100%', height: 56 }} />
        </div>
      ) : projects.length > 0 ? (
        <div style={{ marginBottom: 20 }}>
        <SettingsGroup>
          {projects.map((p) => (
            <button
              key={p.id}
              className="st-row hoverable"
              style={{
                width: '100%',
                background: 'transparent',
                border: 0,
                cursor: 'pointer',
                textAlign: 'left',
                fontFamily: 'var(--sans)',
              }}
              onClick={() => openTopic(p.id)}
            >
              <span
                style={{
                  width: 8,
                  height: 8,
                  borderRadius: '50%',
                  flexShrink: 0,
                  background: p.id === currentProjectId ? 'var(--accent)' : 'var(--border-strong)',
                }}
              />
              <span style={{ flex: 1, minWidth: 0 }}>
                <span
                  style={{
                    display: 'block',
                    fontSize: 13,
                    fontWeight: 500,
                    color: 'var(--text)',
                    overflow: 'hidden',
                    textOverflow: 'ellipsis',
                    whiteSpace: 'nowrap',
                  }}
                >
                  {p.name}
                </span>
                {p.created_at && (
                  <span style={{ display: 'block', fontSize: 12, color: 'var(--text-3)', marginTop: 2 }}>
                    {tr('创建于', 'Created')} {fmtTime(p.created_at)}
                  </span>
                )}
              </span>
              <Icon name="arrow" size={14} style={{ color: 'var(--text-3)', flexShrink: 0 }} />
            </button>
          ))}
        </SettingsGroup>
        </div>
      ) : (
        <div style={{ padding: '20px 0', textAlign: 'center', marginBottom: 8 }}>
          <div style={{ fontSize: 13, color: 'var(--text-3)', lineHeight: 1.6 }}>
            {tr('还没有课题，填写名称和一句话描述即可创建。', 'No topics yet. A name and one sentence is all you need.')}
          </div>
        </div>
      )}

      <div style={{ textAlign: 'center' }}>
        <div className="row gap8" style={{ justifyContent: 'center', flexWrap: 'wrap' }}>
          <button className="btn btn-primary" onClick={() => navigate('/projects/new')}>
            <Icon name="plus" size={14} />
            {tr('新建课题', 'New topic')}
          </button>
          <button className="btn btn-ghost" onClick={createSample} disabled={creatingSample}>
            <Icon name="sparkle" size={14} />
            {creatingSample ? tr('正在创建…', 'Creating…') : tr('创建示例课题', 'Create sample topic')}
          </button>
        </div>
      </div>

      {/* —— 不依赖课题的功能入口 —— */}
      <div style={{ marginTop: 40 }}>
        <div
          style={{
            display: 'grid',
            gridTemplateColumns: 'repeat(auto-fit, minmax(240px, 1fr))',
            gap: 12,
          }}
        >
          {ENTRIES.map((e) => {
            const [title, desc] = getLang() === 'en' ? e.en : e.zh;
            return (
              <button
                key={e.to}
                className="card card-pad hoverable"
                style={{
                  display: 'flex',
                  alignItems: 'flex-start',
                  gap: 12,
                  textAlign: 'left',
                  fontFamily: 'var(--sans)',
                  cursor: 'pointer',
                }}
                onClick={() => navigate(e.to)}
              >
                <span
                  style={{
                    width: 32,
                    height: 32,
                    borderRadius: 9,
                    flexShrink: 0,
                    background: 'var(--accent-soft)',
                    color: 'var(--accent)',
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'center',
                  }}
                >
                  <Icon name={e.icon} size={16} />
                </span>
                <span style={{ flex: 1, minWidth: 0 }}>
                  <span
                    style={{
                      display: 'flex',
                      alignItems: 'center',
                      gap: 6,
                      fontSize: 13,
                      fontWeight: 600,
                      color: 'var(--text)',
                    }}
                  >
                    {title}
                    <Icon name="arrow" size={12} style={{ color: 'var(--text-4)', flexShrink: 0 }} />
                  </span>
                  <span
                    style={{
                      display: 'block',
                      fontSize: 12,
                      color: 'var(--text-3)',
                      lineHeight: 1.6,
                      marginTop: 5,
                    }}
                  >
                    {desc}
                  </span>
                </span>
              </button>
            );
          })}
        </div>
      </div>
    </div>
  );
}
