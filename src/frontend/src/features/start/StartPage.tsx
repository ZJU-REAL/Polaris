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
    zh: ['实验室文献库', '全实验室共享的方向文献库，可以浏览、检索、和文献对话。'],
    en: ['Lab libraries', 'Shared topic libraries for the whole lab — browse, search and chat with the papers.'],
  },
  {
    to: '/daily',
    icon: 'heart',
    zh: ['每日新论文', 'arXiv 每日新提交，可以点赞、收录进文献库、看 AI 解读。'],
    en: ['Daily papers', "Fresh arXiv submissions every day — like them, add them to a library, read the AI digest."],
  },
  {
    to: '/lab',
    icon: 'flask',
    zh: ['实验室工作台', '实验室资源概况，以及全部 AI 任务的运行情况。'],
    en: ['Lab workbench', 'Lab resources at a glance, plus every AI task and how it is running.'],
  },
  {
    to: '/library',
    icon: 'bookmark',
    zh: ['我的文献库', '你自己收藏的论文，随手存、随时读。'],
    en: ['My library', 'The papers you saved yourself — stash now, read later.'],
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
    statement: 'How models improve themselves — capability limits and safety.',
  },
};
/** 判重用：两种语言的名字都算「已经有示例课题了」。 */
const SAMPLE_NAMES = [SAMPLE_TOPIC.zh.name, SAMPLE_TOPIC.en.name];

/** 示例课题绑定的文献库（按名字匹配可见库；没有就退回第一个公共库）。 */
const SAMPLE_LIBRARY_NAMES = ['Recursive Self-Improvement', 'RSI'];

/** 示例课题的相关研究里预置的三篇论文（arXiv id）。 */
const SAMPLE_PAPER_IDS = ['2506.10943', '2505.03335', '2203.14465'];

export function StartPage() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { projects, isLoading, currentProjectId, setCurrentProjectId } = useProject();
  const [creatingSample, setCreatingSample] = useState(false);

  // 示例课题的语料：挑一个已激活的公共库；没有就空着建（允许无语料）
  const librariesQuery = useLibraries();

  function openTopic(id: string) {
    setCurrentProjectId(id);
    navigate(topicPath(id));
  }

  /** 一键创建示例课题；已经有同名的就直接进去，不重复建。 */
  async function createSample() {
    const existing = projects.find((p) => SAMPLE_NAMES.includes(p.name));
    if (existing) {
      toast(tr('示例课题已经有了，直接进入。', 'You already have the sample topic — opening it.'), 'info');
      openTopic(existing.id);
      return;
    }
    const preset = getLang() === 'en' ? SAMPLE_TOPIC.en : SAMPLE_TOPIC.zh;
    // 语料优先绑 RSI 文献库（示例课题就是讲这个方向）；找不到才退回任一公共库
    const visible = librariesQuery.data ?? [];
    const active = visible.filter((l) => l.status === 'active');
    const lib =
      active.find((l) => SAMPLE_LIBRARY_NAMES.some((n) => l.name.toLowerCase().includes(n.toLowerCase()))) ??
      active.find((l) => l.is_public);
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
        `${tr('示例课题已创建', 'Sample topic created')}${
          lib ? `${tr('，语料：', ', corpus: ')}${lib.name}` : ''
        }${okCount > 0 ? tr(`，已放入 ${okCount} 篇论文`, `, ${okCount} papers added`) : ''}`,
        'ok',
      );
      openTopic(created.id);
    } catch (e) {
      toast(`${tr('创建失败：', 'Create failed: ')}${e instanceof Error ? e.message : String(e)}`, 'error');
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
        <h1 style={{ fontSize: 20, fontWeight: 700, margin: 0 }}>
          {tr('选择或创建课题', 'Pick or create a topic')}
        </h1>
      </div>

      {isLoading ? (
        <div className="col gap10">
          <div className="skel" style={{ width: '100%', height: 56 }} />
          <div className="skel" style={{ width: '100%', height: 56 }} />
        </div>
      ) : projects.length > 0 ? (
        <div className="col gap10" style={{ marginBottom: 20 }}>
          {projects.map((p) => (
            <button
              key={p.id}
              className="card hoverable"
              style={{
                display: 'flex',
                alignItems: 'center',
                gap: 12,
                width: '100%',
                padding: '14px 18px',
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
                    fontSize: 14,
                    fontWeight: 640,
                    color: 'var(--text)',
                    overflow: 'hidden',
                    textOverflow: 'ellipsis',
                    whiteSpace: 'nowrap',
                  }}
                >
                  {p.name}
                </span>
                {p.created_at && (
                  <span className="mono" style={{ display: 'block', fontSize: 10.5, color: 'var(--text-4)', marginTop: 3 }}>
                    {tr('创建于', 'Created')} {fmtTime(p.created_at)}
                  </span>
                )}
              </span>
              <Icon name="arrow" size={14} style={{ color: 'var(--text-4)', flexShrink: 0 }} />
            </button>
          ))}
        </div>
      ) : (
        <div className="card" style={{ padding: '28px 24px', textAlign: 'center', marginBottom: 20 }}>
          <div style={{ fontSize: 13, color: 'var(--text-3)', lineHeight: 1.6 }}>
            {tr('你还没有课题。只需名称和一句话描述，30 秒创建第一个课题。', 'You have no topics yet. With just a name and one sentence, your first topic is 30 seconds away.')}
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
            {creatingSample ? tr('创建中…', 'Creating…') : tr('创建示例课题', 'Create a sample topic')}
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
                      fontSize: 13.5,
                      fontWeight: 640,
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
