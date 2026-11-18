import { useMemo, useState, type ReactNode } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Icon } from '../../components/ui/Icon';
import { ErrorState, LoadingState } from '../../components/ui/EmptyState';
import { Modal } from '../../components/ui/Modal';
import { FormField } from '../../components/ui/FormField';
import { ConfirmModal } from '../../components/ui/ConfirmModal';
import { DotsMenu } from '../../components/ui/DotsMenu';
import { toast } from '../../components/ui/Toast';
import { parseApiDate } from '../../lib/format';
import { errorText } from '../../lib/errors';
import { api, ApiError, type DirectionLibrarySummary, type LibraryEntry } from '../../lib/api';
import { tr } from '../../lib/i18n';
import { useDirections } from '../deepresearch/directions';
import { PaperRow, shortAuthors } from '../shared/PaperRow';
import { StatementInterview } from './StatementInterview';
import { useLibraries, libraryPath } from './hooks';
import { DisciplineSelect } from './DisciplineSelect';
import { AddPapersEntry } from './AddPapersEntry';
import {
  InclusionSettingsForm,
  hasInclusionKeywords,
  keywordsFromInclusion,
  type InclusionValue,
} from './InclusionSettingsForm';
import './papersHome.css';

/* ============================================================
   /libraries — 论文首页。侧栏「论文」进来就是这一页：

     课题的文献库   研究课题各自收集的文献库（名称 · 篇数 · 更新时间 · 课题）
     其他文献库     自己新建、不属于课题的
     我的文献库     收藏和最近读过的几篇，「全部打开」进 /library

   只有一个主按钮「添加论文」：先选加到哪里，再用那里原有的添加方式。
   ============================================================ */

const EMPTY_INCLUSION: InclusionValue = {
  sources: [],
  arxiv_categories: [],
  include: [],
  exclude: [],
  rubric: [],
  anchors: [],
};

/**
 * 新建文献库弹窗（P9b：任意登录用户可建）。名称 + 一句话说明必填；
 * 收录设置（分类 / 关键词 / 锚点论文 / 打分标准）共用 InclusionSettingsForm，
 * 可点「AI 自动生成」按名称+说明推荐一整套。提交后立即可用，跳详情页即可开始抓取。
 */
function NewLibraryModal({ open, onClose }: { open: boolean; onClose: () => void }) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [name, setName] = useState('');
  const [statement, setStatement] = useState('');
  // 学科决定论文按哪套字段抽取。建完库再去设置里改也行，但那之前抽过的论文
  // 已经按通用（机器学习形状的）口径存下来了——所以这个问题属于建库时
  const [discipline, setDiscipline] = useState('');
  const [interviewOpen, setInterviewOpen] = useState(false);
  const [incl, setIncl] = useState<InclusionValue>(EMPTY_INCLUSION);
  // 默认来源跟学科走（#821）：选「计算机科学」给 arXiv + cs.* 快捷项，选临床给 PubMed，
  // 不选学科给覆盖全学科的通用来源。用户动过来源勾选之后以他的为准（incl.sources 非空）
  const packsQuery = useQuery({ queryKey: ['disciplines'], queryFn: () => api.listDisciplines(), retry: false });
  const generalQuery = useQuery({
    queryKey: ['disciplines', 'defaults'],
    queryFn: () => api.getDisciplineDefaults(),
    retry: false,
  });
  const pack = (packsQuery.data ?? []).find((p) => p.name === discipline);
  // 包声明了来源就用它的；否则是部署默认（管理员在文献检索设置里配的）。两者都取不到时
  // 留空，不在前端编一个默认值——建库时服务端会按同样的规则补上
  const defaultSources = pack?.sources.length ? pack.sources : generalQuery.data?.sources ?? [];


  const mutation = useMutation({
    mutationFn: (input: Parameters<typeof api.createLibrary>[0]) => api.createLibrary(input),
    onSuccess: (lib) => {
      toast(tr('已创建文献库', 'Library created'), 'ok');
      void queryClient.invalidateQueries({ queryKey: ['libraries'] });
      onClose();
      navigate(libraryPath(lib.id));
    },
    onError: (err) => {
      toast(`${tr('无法创建：', 'Couldn’t create: ')}${err instanceof Error ? err.message : String(err)}`, 'error');
    },
  });

  function submit() {
    if (!name.trim()) {
      toast(tr('请填写文献库名称', 'Enter a library name'), 'info');
      return;
    }
    if (!statement.trim()) {
      toast(tr('请填写方向说明', 'Enter a scope'), 'info');
      return;
    }
    // 只选了来源、没填分类和关键词，也必须把 keywords 发出去：
    // 漏掉它等于用户挑的 PubMed 被悄悄丢掉，建出来的库还是抓 arXiv
    const keywords = hasInclusionKeywords(incl) ? keywordsFromInclusion(incl) : undefined;
    const anchors = incl.anchors
      .filter((a) => a.title.trim() || (a.arxiv_id ?? '').trim())
      .map((a) => ({
        title: a.title.trim(),
        ...(a.arxiv_id?.trim() ? { arxiv_id: a.arxiv_id.trim() } : {}),
        ...(a.reason?.trim() ? { reason: a.reason.trim() } : {}),
      }));
    const rubric = incl.rubric.filter((r) => r.name.trim());
    mutation.mutate({
      name: name.trim(),
      statement: statement.trim(),
      ...(discipline ? { discipline } : {}),
      ...(anchors.length > 0 ? { anchors } : {}),
      ...(keywords ? { keywords } : {}),
      ...(rubric.length > 0 ? { rubric } : {}),
    });
  }

  return (
    <Modal
      open={open}
      onClose={onClose}
      title={tr('新建文献库', 'New library')}
      width={640}
      footer={
        <>
          <button className="btn btn-ghost sm" onClick={onClose}>{tr('取消', 'Cancel')}</button>
          <button className="btn btn-primary sm" disabled={mutation.isPending} onClick={submit}>
            {mutation.isPending ? tr('创建中…', 'Creating…') : tr('创建文献库', 'Create library')}
          </button>
        </>
      }
    >
      <div style={{ marginTop: 4 }}>
        <FormField label={tr('名称', 'Name')}>
          <input className="input" value={name} onChange={(e) => setName(e.target.value)}
            placeholder={tr('例如 稀疏注意力', 'e.g. Sparse attention')} />
        </FormField>
        <FormField
          label={tr('方向说明', 'Scope')}
          hint={tr('用来判断论文是否相关', 'Used to judge which papers are relevant')}
        >
          <div className="row gap8" style={{ alignItems: 'center', marginBottom: 6 }}>
            <button
              className="btn btn-soft sm"
              disabled={!name.trim()}
              title={
                name.trim()
                  ? tr('回答几个问题，生成方向说明', 'Answer a few questions to draft the scope')
                  : tr('先填名称', 'Enter a name first')
              }
              onClick={() => setInterviewOpen(true)}
            >
              <Icon name="sparkle" size={12} />
              {tr('AI 帮写', 'Draft with AI')}
            </button>
          </div>
          <textarea className="textarea" rows={2} value={statement} onChange={(e) => setStatement(e.target.value)}
            placeholder={tr(
              '例如 Long-running LLM agents: memory compaction, error recovery, long-horizon evaluation. No pure prompt engineering.',
              'e.g. Long-running LLM agents: memory compaction, error recovery, long-horizon evaluation. No pure prompt engineering.',
            )} />
          <div className="muted" style={{ fontSize: 11.5, lineHeight: 1.5, marginTop: 4 }}>
            {tr(
              '建议用英文写明研究问题、对象和偏好的方法，匹配更准。',
              'Write it in English and name the question, subject and preferred methods for better matches.',
            )}
          </div>
        </FormField>
        <div className="hr" style={{ margin: '4px 0 16px' }} />
        <FormField label={tr('学科', 'Discipline')}>
          <DisciplineSelect value={discipline} onChange={setDiscipline} />
          <div className="muted" style={{ fontSize: 11.5, lineHeight: 1.5, marginTop: 4 }}>
            {tr(
              '决定方法卡提取哪些字段。',
              'Sets the fields extracted into method cards.',
            )}
          </div>
        </FormField>
        <div className="hr" style={{ margin: '4px 0 16px' }} />
        <InclusionSettingsForm
          value={incl}
          onChange={setIncl}
          showRubric
          defaultSources={defaultSources}
          arxivQuickPicks={pack?.arxiv_categories ?? []}
        />
      </div>
      <StatementInterview
        open={interviewOpen}
        topic={name}
        onClose={() => setInterviewOpen(false)}
        onDone={setStatement}
      />
    </Modal>
  );
}

/* ---------------- 论文首页 ---------------- */

function fmtDay(iso: string | null | undefined): string {
  if (!iso) return '';
  const d = parseApiDate(iso);
  return tr(`${d.getMonth() + 1} 月 ${d.getDate()} 日更新`, `updated ${d.toLocaleDateString('en', { month: 'short', day: 'numeric' })}`);
}

/** 一个文献库一行：名称；课题 · 篇数 · 更新时间；右边更多菜单（删除）。 */
function LibraryRow({
  lib,
  topic,
  onOpen,
  onDelete,
}: {
  lib: DirectionLibrarySummary;
  topic: { name: string; href: string } | null;
  onOpen: () => void;
  onDelete: () => void;
}) {
  return (
    <div className="ph-lib" data-testid="home-library">
      <button type="button" className="ph-lib-main" onClick={onOpen}>
        <span className="ph-lib-name" title={lib.name}>
          {lib.name}
        </span>
        <span className="ph-lib-meta">
          {[
            tr(`${lib.paper_count} 篇论文`, lib.paper_count === 1 ? '1 paper' : `${lib.paper_count} papers`),
            lib.concept_count ? tr(`${lib.concept_count} 个概念`, `${lib.concept_count} concepts`) : null,
            fmtDay(lib.last_synced_at ?? lib.updated_at),
          ]
            .filter(Boolean)
            .join(' · ')}
        </span>
      </button>
      {topic && topic.name !== lib.name && (
        <Link className="ph-lib-topic" to={topic.href} title={topic.name}>
          {tr('课题', 'Topic')}「{topic.name}」
        </Link>
      )}
      {topic && topic.name === lib.name && (
        <Link className="ph-lib-topic" to={topic.href}>
          {tr('打开课题', 'Open topic')}
        </Link>
      )}
      <DotsMenu
        plain
        label={tr('这个文献库的更多操作', 'More for this library')}
        items={[
          { key: 'open', label: tr('打开', 'Open'), icon: 'book', onSelect: onOpen },
          { key: 'del', label: tr('删除文献库', 'Delete library'), icon: 'trash', danger: true, separated: true, onSelect: onDelete },
        ]}
      />
    </div>
  );
}

/** 我的文献库的一小段预览：最近几篇。 */
function MinePreview({ tab, title, empty }: { tab: 'saved' | 'history'; title: string; empty: string }) {
  const navigate = useNavigate();
  const q = useQuery({
    queryKey: ['library-entries', tab, 'home'],
    queryFn: () => api.listLibrary({ tab, sort: 'recent', page: 1, size: 4 }),
    retry: false,
  });
  const items: LibraryEntry[] = q.data?.items ?? [];
  return (
    <div className="ph-mine-col" data-testid={`home-mine-${tab}`}>
      <div className="ph-mine-h">
        {title}
        {q.data && <span className="ph-count">{q.data.total}</span>}
      </div>
      {q.isLoading ? (
        <LoadingState rows={2} compact />
      ) : items.length === 0 ? (
        <p className="ph-empty">{empty}</p>
      ) : (
        <div className="ph-mine-list">
          {items.map((e) => (
            <PaperRow
              key={e.id}
              title={e.title}
              meta={[shortAuthors(e.authors), e.year]}
              summary={e.tldr}
              onClick={() => {
                if (e.last_paper_id) navigate(`/papers/${e.last_paper_id}/read`);
                else navigate(`/library?tab=${tab}`);
              }}
            />
          ))}
        </div>
      )}
    </div>
  );
}

export function LibrariesPage() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { data, isLoading, isError, refetch } = useLibraries();
  const { byLibrary, byProject } = useDirections();
  const [createOpen, setCreateOpen] = useState(false);
  const [addOpen, setAddOpen] = useState(false);
  const [pendingDelete, setPendingDelete] = useState<DirectionLibrarySummary | null>(null);
  const libraries = useMemo(
    () => [...(data ?? [])].sort((a, b) => (b.last_synced_at ?? b.updated_at).localeCompare(a.last_synced_at ?? a.updated_at)),
    [data],
  );

  const topicOf = (lib: DirectionLibrarySummary) => {
    const d = byLibrary(lib.id) ?? byProject(lib.project_id);
    return d ? { name: d.name, href: d.href } : null;
  };
  const withTopic = libraries.filter((l) => topicOf(l));
  const others = libraries.filter((l) => !topicOf(l));

  // 删除：409 LIBRARY_HAS_TOPICS 时说明后果再确认一次，带 force 重删
  const [forceFor, setForceFor] = useState<DirectionLibrarySummary | null>(null);
  const deleteMutation = useMutation({
    mutationFn: async ({ lib, force }: { lib: DirectionLibrarySummary; force: boolean }) => api.deleteLibrary(lib.id, force),
    onSuccess: (_r, { lib }) => {
      toast(tr(`已删除「${lib.name}」`, `Deleted “${lib.name}”`), 'ok');
      void queryClient.invalidateQueries({ queryKey: ['libraries'] });
    },
    onError: (e, { lib, force }) => {
      if (!force && e instanceof ApiError && e.status === 409 && e.message.includes('LIBRARY_HAS_TOPICS')) {
        setForceFor(lib);
        return;
      }
      toast(`${tr('无法删除：', 'Couldn’t delete: ')}${errorText(e)}`, 'error');
    },
  });

  const section = (title: string, list: DirectionLibrarySummary[], testId: string, empty?: ReactNode) => (
    <section className="ph-section" data-testid={testId}>
      <h2 className="ph-h">{title}</h2>
      {list.length === 0 ? (
        empty
      ) : (
        <div className="ph-libs">
          {list.map((lib) => (
            <LibraryRow
              key={lib.id}
              lib={lib}
              topic={topicOf(lib)}
              onOpen={() => navigate(libraryPath(lib.id))}
              onDelete={() => setPendingDelete(lib)}
            />
          ))}
        </div>
      )}
    </section>
  );

  return (
    <div className="page fadeup ph-page">
      <header className="ph-head">
        <div>
          <h1 className="ph-title">{tr('论文', 'Papers')}</h1>
          <p className="ph-sub">{tr('课题收集的文献库，和你自己收藏、读过的论文。', 'Libraries your topics collected, plus papers you saved and read.')}</p>
        </div>
        <div className="ph-actions">
          <button type="button" className="btn btn-ghost sm" onClick={() => setCreateOpen(true)} data-testid="home-new-library">
            {tr('新建文献库', 'New library')}
          </button>
          <button type="button" className="btn btn-primary sm" onClick={() => setAddOpen(true)} data-testid="home-add">
            <Icon name="plus" size={13} />
            {tr('添加论文', 'Add papers')}
          </button>
        </div>
      </header>

      {isLoading ? (
        <LoadingState rows={3} />
      ) : isError ? (
        <ErrorState title={tr('无法加载文献库，请确认本机引擎正在运行', 'Couldn’t load libraries. Check that the local engine is running.')} onRetry={() => void refetch()} />
      ) : (
        <>
          {section(
            tr('课题的文献库', 'Topic libraries'),
            withTopic,
            'home-topic-libraries',
            <p className="ph-empty">
              {tr('开始一次研究时，会为课题建一个文献库。', 'Starting a research builds a library for its topic.')}{' '}
              <Link to="/research">{tr('开始研究', 'Start a research')}</Link>
            </p>,
          )}
          {others.length > 0 && section(tr('其他文献库', 'Other libraries'), others, 'home-other-libraries')}
        </>
      )}

      <section className="ph-section" data-testid="home-mine">
        <div className="ph-h-row">
          <h2 className="ph-h">{tr('我的文献库', 'My library')}</h2>
          <Link className="ph-link" to="/library" data-testid="home-open-mine">
            {tr('全部打开', 'Open')}
            <Icon name="arrow" size={11} />
          </Link>
        </div>
        <div className="ph-mine">
          <MinePreview tab="saved" title={tr('收藏', 'Saved')} empty={tr('还没有收藏。在论文详情里点「保存到…」就会出现在这里。', 'Nothing saved yet. Use “Save to…” on a paper.')} />
          <MinePreview tab="history" title={tr('最近读过', 'Recently read')} empty={tr('还没有读过的论文。', 'No papers read yet.')} />
        </div>
      </section>

      <NewLibraryModal open={createOpen} onClose={() => setCreateOpen(false)} />
      <AddPapersEntry open={addOpen} onClose={() => setAddOpen(false)} libraries={libraries} />
      <ConfirmModal
        open={!!pendingDelete}
        onClose={() => setPendingDelete(null)}
        title={tr(`删除文献库「${pendingDelete?.name ?? ''}」？`, `Delete library “${pendingDelete?.name ?? ''}”?`)}
        message={tr(
          `这个文献库和它收录的 ${pendingDelete?.paper_count ?? 0} 篇论文一起删除，无法恢复。`,
          `The library and the ${pendingDelete?.paper_count ?? 0} papers it holds are deleted. This can’t be undone.`,
        )}
        confirmText={tr('删除', 'Delete')}
        danger
        onConfirm={() => {
          if (pendingDelete) deleteMutation.mutate({ lib: pendingDelete, force: false });
          setPendingDelete(null);
        }}
      />
      <ConfirmModal
        open={!!forceFor}
        onClose={() => setForceFor(null)}
        title={tr(`「${forceFor?.name ?? ''}」属于一个课题，仍要删除？`, `“${forceFor?.name ?? ''}” belongs to a topic. Delete it anyway?`)}
        message={tr('课题还在，只是不再有这个文献库。', 'The topic stays; it just loses this library.')}
        confirmText={tr('仍然删除', 'Delete anyway')}
        danger
        onConfirm={() => {
          if (forceFor) deleteMutation.mutate({ lib: forceFor, force: true });
          setForceFor(null);
        }}
      />
    </div>
  );
}
