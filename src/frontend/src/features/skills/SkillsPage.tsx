import { useMemo, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Icon } from '../../components/ui/Icon';
import { PageHead } from '../../components/ui/PageHead';
import { Segmented } from '../../components/ui/Segmented';
import { EmptyState } from '../../components/ui/EmptyState';
import { Modal } from '../../components/ui/Modal';
import { FormField } from '../../components/ui/FormField';
import { toast } from '../../components/ui/Toast';
import { tr } from '../../lib/i18n';
import { useProject } from '../../app/project';
import {
  api,
  isAdmin,
  skillTargetLabel,
  skillKindLabel,
  SKILL_TARGETS,
  type ProjectSkillRead,
  type SkillDetail,
  type SkillKind,
  type SkillListingRead,
  type SkillExportData,
  type SkillManifest,
  type SkillRead,
  type SkillTestResult,
} from '../../lib/api';

/** 下载 JSON 文件（技能包导出）。 */
function downloadJson(filename: string, data: unknown): void {
  const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}

/* ============================================================
   /skills — 技能库：内置/我的技能列表 + 启用到当前研究方向。
   - 技能 = 各环节 AI 的补充判断标准（指引/评分标准/评审人设/流程模板）
   - 详情弹窗：全文预览 / 试运行 / 启用 / 复制为我的 / 编辑（追加版本）
   - 流程模板可直接「运行此流程」→ 创建 AI 任务
   ============================================================ */

type ScopeFilter = 'all' | 'builtin' | 'mine';
type KindFilter = 'all' | SkillKind;

/** 文案在渲染处再 tr（模块级求值不会随语言切换更新）。 */
const KIND_FILTERS: { v: KindFilter; zh: string; en: string }[] = [
  { v: 'all', zh: '全部类型', en: 'All kinds' },
  { v: 'guidance', zh: '指引', en: 'Guidance' },
  { v: 'rubric', zh: '评分标准', en: 'Rubrics' },
  { v: 'persona', zh: '评审人设', en: 'Personas' },
  { v: 'workflow', zh: '流程模板', en: 'Workflows' },
];

const KIND_COLOR: Record<SkillKind, { bg: string; tx: string }> = {
  guidance: { bg: 'var(--info-bg)', tx: 'var(--info-tx)' },
  rubric: { bg: 'var(--warn-bg)', tx: 'var(--warn-tx)' },
  persona: { bg: 'var(--violet-bg)', tx: 'var(--violet-tx)' },
  workflow: { bg: 'var(--ok-bg)', tx: 'var(--ok-tx)' },
};

function KindPill({ kind }: { kind: SkillKind }) {
  const c = KIND_COLOR[kind];
  return (
    <span className="pill sm" style={{ background: c.bg, color: c.tx, flexShrink: 0 }}>
      {skillKindLabel(kind)}
    </span>
  );
}

function errMsg(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}

// ---- 技能卡片 ----

function SkillCard({ s, onOpen }: { s: SkillRead; onOpen: () => void }) {
  return (
    <button
      className="card"
      onClick={onOpen}
      style={{ display: 'block', width: '100%', textAlign: 'left', padding: '14px 16px', cursor: 'pointer' }}
    >
      <div className="row gap8" style={{ alignItems: 'center' }}>
        <span style={{ fontSize: 13.5, fontWeight: 650 }}>{s.name}</span>
        <KindPill kind={s.kind} />
        {s.scope === 'builtin' && (
          <span className="pill sm" style={{ background: 'var(--surface-3)', color: 'var(--text-3)' }}>
            {tr('内置', 'Built-in')}
          </span>
        )}
        <span className="mono" style={{ marginLeft: 'auto', fontSize: 10.5, color: 'var(--text-3)' }}>
          {s.slug}
        </span>
      </div>
      {s.description && (
        <div style={{ fontSize: 12, color: 'var(--text-2)', marginTop: 6, lineHeight: 1.5 }}>{s.description}</div>
      )}
    </button>
  );
}

// ---- 详情弹窗 ----

function SkillDetailModal({
  skillId,
  onClose,
}: {
  skillId: string;
  onClose: () => void;
}) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { currentProjectId } = useProject();
  const [test, setTest] = useState<SkillTestResult | null>(null);
  const [enableTarget, setEnableTarget] = useState<string>('');
  const [editOpen, setEditOpen] = useState(false);
  const [runGoal, setRunGoal] = useState('');

  const { data: skill } = useQuery({
    queryKey: ['skill', skillId],
    queryFn: () => api.getSkill(skillId),
  });

  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: ['skills'] });
    void queryClient.invalidateQueries({ queryKey: ['skill', skillId] });
    void queryClient.invalidateQueries({ queryKey: ['project-skills'] });
  };

  const testMutation = useMutation({
    mutationFn: () => api.testSkill(skillId),
    onSuccess: setTest,
    onError: (e) => toast(`${tr('试运行失败', 'Test run failed')}：${errMsg(e)}`, 'error'),
  });
  const forkMutation = useMutation({
    mutationFn: () => api.forkSkill(skillId),
    onSuccess: (fork) => {
      toast(`${tr('已复制为我的技能', 'Copied to my skills')}：${fork.name}`, 'ok');
      invalidate();
    },
    onError: (e) => toast(`${tr('复制失败', 'Copy failed')}：${errMsg(e)}`, 'error'),
  });
  const exportMutation = useMutation({
    mutationFn: () => api.exportSkill(skillId),
    onSuccess: (data) => {
      downloadJson(`${data.slug}.polaris-skill.json`, data);
      toast(tr('技能包已下载', 'Skill package downloaded'), 'ok');
    },
    onError: (e) => toast(`${tr('导出失败', 'Export failed')}：${errMsg(e)}`, 'error'),
  });
  const publishMutation = useMutation({
    mutationFn: () => api.publishSkill(skillId),
    onSuccess: () => {
      toast(tr('已提交发布，等待管理员审核', 'Submitted for admin review'), 'ok');
      void queryClient.invalidateQueries({ queryKey: ['market-skills'] });
    },
    onError: (e) =>
      toast(e instanceof Error && e.message === 'ALREADY_LISTED' ? tr('该技能已在市场（或待审核中）', 'Already on the market (or pending review)') : `${tr('发布失败', 'Publish failed')}：${errMsg(e)}`, 'error'),
  });
  const archiveMutation = useMutation({
    mutationFn: () => api.archiveSkill(skillId),
    onSuccess: () => {
      toast(tr('技能已删除', 'Skill deleted'), 'ok');
      invalidate();
      onClose();
    },
    onError: (e) => toast(`${tr('删除失败', 'Delete failed')}：${errMsg(e)}`, 'error'),
  });
  const enableMutation = useMutation({
    mutationFn: (target: string) =>
      api.enableProjectSkill(currentProjectId!, { skill_id: skillId, target }),
    onSuccess: (row) => {
      toast(`${tr('已启用到当前方向', 'Enabled for current direction')}：${skillTargetLabel(row.target)}`, 'ok');
      invalidate();
    },
    onError: (e) =>
      toast(e instanceof Error && e.message === 'SKILL_ALREADY_ENABLED' ? tr('该环节已启用过此技能', 'Already enabled for that stage') : `${tr('启用失败', 'Enable failed')}：${errMsg(e)}`, 'error'),
  });
  const runMutation = useMutation({
    mutationFn: () => api.runWorkflowSkill(skillId, { project_id: currentProjectId!, goal: runGoal.trim() }),
    onSuccess: (run) => {
      toast(tr('AI 任务已创建', 'AI task created'), 'ok');
      void queryClient.invalidateQueries({ queryKey: ['voyages'] });
      onClose();
      navigate(`/voyages/${run.id}`);
    },
    onError: (e) => toast(`${tr('运行失败', 'Run failed')}：${errMsg(e)}`, 'error'),
  });

  if (!skill) return null;
  const version = skill.current_version;
  const targets = version?.manifest.targets ?? [];
  const mine = skill.scope !== 'builtin';

  return (
    <Modal
      open
      onClose={onClose}
      width={640}
      title={
        <>
          {skill.name}
          <KindPill kind={skill.kind} />
        </>
      }
      sub={`${skill.slug} · v${version?.version ?? '—'} · ${skill.scope === 'builtin' ? tr('内置技能', 'Built-in skill') : tr('我的技能', 'My skill')}`}
      footer={
        <>
          {mine && (
            <button
              className="btn btn-ghost"
              style={{ marginRight: 'auto', color: 'var(--danger, #c0392b)' }}
              onClick={() => {
                if (window.confirm(tr('删除这个技能？已启用的项目会同时失效（进行中的任务不受影响）。', 'Delete this skill? Projects using it will lose it (running tasks are unaffected).'))) {
                  archiveMutation.mutate();
                }
              }}
            >
              <Icon name="trash" size={13} /> {tr('删除', 'Delete')}
            </button>
          )}
          <button className="btn btn-ghost" disabled={exportMutation.isPending} onClick={() => exportMutation.mutate()}>
            {tr('导出', 'Export')}
          </button>
          {skill.scope === 'builtin' && (
            <button className="btn btn-soft" disabled={forkMutation.isPending} onClick={() => forkMutation.mutate()}>
              {tr('复制为我的技能', 'Copy to my skills')}
            </button>
          )}
          {mine && (
            <button className="btn btn-soft" onClick={() => setEditOpen(true)}>
              {tr('编辑（存为新版本）', 'Edit (save as new version)')}
            </button>
          )}
          {mine && (
            <button className="btn btn-soft" disabled={publishMutation.isPending} onClick={() => publishMutation.mutate()}>
              {tr('发布到市场', 'Publish to market')}
            </button>
          )}
          {skill.kind !== 'workflow' && (
            <button className="btn btn-soft" disabled={testMutation.isPending} onClick={() => testMutation.mutate()}>
              <Icon name="play" size={13} /> {testMutation.isPending ? tr('运行中…', 'Running…') : tr('试运行', 'Test run')}
            </button>
          )}
        </>
      }
    >
      {skill.description && <p style={{ fontSize: 12.5, color: 'var(--text-2)', marginBottom: 12 }}>{skill.description}</p>}

      {/* 适用环节 + 启用 */}
      <div className="row gap8" style={{ flexWrap: 'wrap', marginBottom: 14, alignItems: 'center' }}>
        <span style={{ fontSize: 11.5, color: 'var(--text-3)' }}>{tr('适用环节：', 'Applies to:')}</span>
        {targets.map((t) => (
          <span key={t} className="pill sm" style={{ background: 'var(--surface-3)', color: 'var(--text-2)' }}>
            {skillTargetLabel(t)}
          </span>
        ))}
        {skill.kind !== 'workflow' && currentProjectId && targets.length > 0 && (
          <span className="row gap8" style={{ marginLeft: 'auto' }}>
            {targets.length > 1 && (
              <select className="input" style={{ width: 160 }} value={enableTarget} onChange={(e) => setEnableTarget(e.target.value)}>
                {targets.map((t) => (
                  <option key={t} value={t}>
                    {skillTargetLabel(t)}
                  </option>
                ))}
              </select>
            )}
            <button
              className="btn btn-primary"
              disabled={enableMutation.isPending}
              onClick={() => {
                const t = enableTarget || targets[0];
                if (t) enableMutation.mutate(t);
              }}
            >
              {tr('启用到当前方向', 'Enable for current direction')}
            </button>
          </span>
        )}
      </div>

      {/* workflow：运行此流程 */}
      {skill.kind === 'workflow' && (
        <div className="card" style={{ padding: '12px 14px', marginBottom: 14, background: 'var(--surface-2)' }}>
          <FormField label={tr('任务目标', 'Goal')} hint={tr('流程各步骤可引用 {goal} 模板变量', 'Steps can reference the {goal} template variable')}>
            <textarea
              className="textarea"
              rows={2}
              placeholder={tr('例如：综述 LLM 推理加速的研究现状', 'e.g. survey the state of LLM inference acceleration')}
              value={runGoal}
              onChange={(e) => setRunGoal(e.target.value)}
            />
          </FormField>
          <div className="row" style={{ justifyContent: 'flex-end', marginTop: 8 }}>
            <button
              className="btn btn-primary"
              disabled={!currentProjectId || !runGoal.trim() || runMutation.isPending}
              onClick={() => runMutation.mutate()}
            >
              <Icon name="play" size={13} /> {tr('运行此流程', 'Run this workflow')}
            </button>
          </div>
        </div>
      )}

      {/* persona 预览 */}
      {skill.kind === 'persona' && (version?.manifest.personas?.length ?? 0) > 0 && (
        <div style={{ marginBottom: 14 }}>
          {version!.manifest.personas!.map((p) => (
            <div key={p.name} className="row gap8" style={{ padding: '6px 0', alignItems: 'baseline' }}>
              <span style={{ fontSize: 12.5, fontWeight: 650, flexShrink: 0 }}>{p.name}</span>
              <span style={{ fontSize: 12, color: 'var(--text-2)' }}>{p.stance}</span>
            </div>
          ))}
        </div>
      )}

      {/* workflow 步骤预览 */}
      {skill.kind === 'workflow' && (version?.manifest.steps?.length ?? 0) > 0 && (
        <ol style={{ margin: '0 0 14px 18px', padding: 0 }}>
          {version!.manifest.steps!.map((s, i) => (
            <li key={i} style={{ fontSize: 12.5, color: 'var(--text-2)', marginBottom: 4 }}>
              {String(s.title ?? '')}
              <span className="mono" style={{ fontSize: 10.5, color: 'var(--text-3)', marginLeft: 6 }}>
                {String(s.action ?? '')}
              </span>
            </li>
          ))}
        </ol>
      )}

      {/* 正文 */}
      {version && (
        <pre
          className="scroll"
          style={{
            whiteSpace: 'pre-wrap',
            fontSize: 12,
            lineHeight: 1.65,
            background: 'var(--surface-2)',
            border: '0.5px solid var(--border)',
            borderRadius: 8,
            padding: '12px 14px',
            maxHeight: 260,
            overflowY: 'auto',
          }}
        >
          {version.body}
        </pre>
      )}

      {/* 试运行结果 */}
      {test && (
        <div style={{ marginTop: 14 }}>
          <div style={{ fontSize: 11.5, color: 'var(--text-3)', marginBottom: 6 }}>
            {tr('试运行输出', 'Test output')}{test.model ? tr(`（模型：${test.model}）`, ` (model: ${test.model})`) : ''}
          </div>
          <pre
            style={{
              whiteSpace: 'pre-wrap',
              fontSize: 12,
              lineHeight: 1.6,
              background: 'var(--accent-soft)',
              borderRadius: 8,
              padding: '12px 14px',
              maxHeight: 200,
              overflowY: 'auto',
            }}
          >
            {test.output ?? test.rendered}
          </pre>
        </div>
      )}

      {editOpen && <SkillEditModal skill={skill} onClose={() => setEditOpen(false)} onSaved={invalidate} />}
    </Modal>
  );
}

// ---- 编辑（追加版本）----

function SkillEditModal({
  skill,
  onClose,
  onSaved,
}: {
  skill: SkillDetail;
  onClose: () => void;
  onSaved: () => void;
}) {
  const [body, setBody] = useState(skill.current_version?.body ?? '');
  const [changelog, setChangelog] = useState('');
  const [manifestText, setManifestText] = useState(JSON.stringify(skill.current_version?.manifest ?? { targets: [] }, null, 2));
  const [showAdvanced, setShowAdvanced] = useState(false);

  const saveMutation = useMutation({
    mutationFn: () => {
      let manifest: SkillManifest;
      try {
        manifest = JSON.parse(manifestText) as SkillManifest;
      } catch {
        throw new Error(tr('高级设置不是合法 JSON', 'Advanced settings is not valid JSON'));
      }
      return api.addSkillVersion(skill.id, { manifest, body, changelog: changelog || undefined });
    },
    onSuccess: (v) => {
      toast(tr(`已保存为 v${v.version}`, `Saved as v${v.version}`), 'ok');
      onSaved();
      onClose();
    },
    onError: (e) => toast(`${tr('保存失败', 'Save failed')}：${errMsg(e)}`, 'error'),
  });

  return (
    <Modal
      open
      onClose={onClose}
      width={620}
      title={`${tr('编辑', 'Edit')}：${skill.name}`}
      sub={tr('保存后生成新版本；已按旧版本固定的项目不受影响', 'Saving creates a new version; projects pinned to older versions are unaffected')}
      footer={
        <>
          <button className="btn btn-ghost" onClick={onClose}>
            {tr('取消', 'Cancel')}
          </button>
          <button className="btn btn-primary" disabled={!body.trim() || saveMutation.isPending} onClick={() => saveMutation.mutate()}>
            {tr('保存为新版本', 'Save as new version')}
          </button>
        </>
      }
    >
      <FormField label={tr('技能内容', 'Body')} hint={tr('Markdown；这段文字会注入对应环节的 AI 指令', 'Markdown; injected into the AI instructions of the target stages')}>
        <textarea className="textarea mono" rows={12} value={body} onChange={(e) => setBody(e.target.value)} />
      </FormField>
      <FormField label={tr('修改说明', 'Changelog')}>
        <input className="input" value={changelog} onChange={(e) => setChangelog(e.target.value)} placeholder={tr('这次改了什么（可选）', 'What changed (optional)')} />
      </FormField>
      <button className="btn btn-ghost" style={{ fontSize: 12 }} onClick={() => setShowAdvanced((v) => !v)}>
        {showAdvanced ? tr('收起高级设置', 'Hide advanced settings') : tr('高级设置（适用环节 / 人设 / 步骤，JSON）', 'Advanced settings (targets / personas / steps, JSON)')}
      </button>
      {showAdvanced && (
        <textarea
          className="textarea mono"
          rows={8}
          style={{ marginTop: 8, fontSize: 11.5 }}
          value={manifestText}
          onChange={(e) => setManifestText(e.target.value)}
        />
      )}
    </Modal>
  );
}

// ---- 新建技能 ----

/** 文案在渲染处再 tr（模块级求值不会随语言切换更新）。 */
const CREATE_KINDS: { v: SkillKind; zh: string; en: string; hintZh: string; hintEn: string }[] = [
  { v: 'guidance', zh: '指引', en: 'Guidance', hintZh: '为某个环节追加做事方式与注意点', hintEn: 'Add working guidelines and caveats for a stage' },
  { v: 'rubric', zh: '评分标准', en: 'Rubric', hintZh: '为打分/评审环节定义评价细则', hintEn: 'Define scoring criteria for review stages' },
];

function SkillCreateModal({ onClose, onCreated }: { onClose: () => void; onCreated: (s: SkillDetail) => void }) {
  const [name, setName] = useState('');
  const [slug, setSlug] = useState('');
  const [kind, setKind] = useState<SkillKind>('guidance');
  const [description, setDescription] = useState('');
  const [targets, setTargets] = useState<string[]>([]);
  const [body, setBody] = useState('');

  const createMutation = useMutation({
    mutationFn: () =>
      api.createSkill({
        slug: slug.trim(),
        kind,
        name: name.trim(),
        description: description.trim() || undefined,
        manifest: { targets },
        body,
      }),
    onSuccess: (s) => {
      toast(`${tr('技能已创建', 'Skill created')}：${s.name}`, 'ok');
      onCreated(s);
      onClose();
    },
    onError: (e) =>
      toast(e instanceof Error && e.message === 'SKILL_SLUG_TAKEN' ? tr('标识已被占用，换一个吧', 'Slug already taken — try another') : `${tr('创建失败', 'Create failed')}：${errMsg(e)}`, 'error'),
  });

  const canSubmit = name.trim() && /^[a-z0-9][a-z0-9-]{1,62}$/.test(slug.trim()) && targets.length > 0 && body.trim();

  return (
    <Modal
      open
      onClose={onClose}
      width={620}
      title={tr('新建技能', 'New skill')}
      sub={tr('评审人设与流程模板建议从内置技能复制后修改', 'For personas and workflows, copy a built-in skill and edit it')}
      footer={
        <>
          <button className="btn btn-ghost" onClick={onClose}>
            {tr('取消', 'Cancel')}
          </button>
          <button className="btn btn-primary" disabled={!canSubmit || createMutation.isPending} onClick={() => createMutation.mutate()}>
            {tr('创建', 'Create')}
          </button>
        </>
      }
    >
      <div className="row gap10">
        <FormField label={tr('名称', 'Name')} style={{ flex: 1 }}>
          <input className="input" value={name} onChange={(e) => setName(e.target.value)} placeholder={tr('如：严格的想法打分标准', 'e.g. strict idea scoring rubric')} />
        </FormField>
        <FormField label={tr('标识', 'Slug')} style={{ flex: 1 }} hint={tr('小写字母/数字/连字符', 'lowercase letters / digits / hyphens')}>
          <input className="input mono" value={slug} onChange={(e) => setSlug(e.target.value)} placeholder="my-scoring-rubric" />
        </FormField>
      </div>
      <FormField label={tr('类型', 'Kind')}>
        <div className="row gap8">
          {CREATE_KINDS.map((k) => (
            <button
              key={k.v}
              className={`btn ${kind === k.v ? 'btn-primary' : 'btn-soft'}`}
              title={tr(k.hintZh, k.hintEn)}
              onClick={() => setKind(k.v)}
            >
              {tr(k.zh, k.en)}
            </button>
          ))}
        </div>
      </FormField>
      <FormField label={tr('适用环节', 'Applies to')} hint={tr('技能会注入这些环节的 AI 指令（可多选）', 'Injected into the AI instructions of these stages (multi-select)')}>
        <div className="row gap8" style={{ flexWrap: 'wrap' }}>
          {SKILL_TARGETS
            .filter((t) => t !== 'navigator.free_plan')
            .map((t) => {
              const on = targets.includes(t);
              return (
                <button
                  key={t}
                  className="pill sm"
                  style={{
                    cursor: 'pointer',
                    background: on ? 'var(--accent-soft)' : 'var(--surface-3)',
                    color: on ? 'var(--accent-text)' : 'var(--text-2)',
                    border: on ? '1px solid var(--accent)' : '1px solid transparent',
                  }}
                  onClick={() => setTargets((prev) => (on ? prev.filter((x) => x !== t) : [...prev, t]))}
                >
                  {skillTargetLabel(t)}
                </button>
              );
            })}
        </div>
      </FormField>
      <FormField label={tr('描述', 'Description')}>
        <input className="input" value={description} onChange={(e) => setDescription(e.target.value)} placeholder={tr('一句话说明这个技能做什么（可选）', 'One line on what this skill does (optional)')} />
      </FormField>
      <FormField label={tr('技能内容', 'Body')} hint={tr('Markdown；写清楚判断标准/注意点，AI 会照此执行', 'Markdown; spell out the criteria — the AI will follow them')}>
        <textarea className="textarea mono" rows={10} value={body} onChange={(e) => setBody(e.target.value)} />
      </FormField>
    </Modal>
  );
}

// ---- 技能市场 ----

function Stars({ avg, count }: { avg: number | null; count: number }) {
  if (!count) return <span style={{ fontSize: 11, color: 'var(--text-3)' }}>{tr('暂无评分', 'No ratings yet')}</span>;
  return (
    <span style={{ fontSize: 11.5, color: 'var(--warn-tx)' }}>
      ★ {avg?.toFixed(1)}
      <span style={{ color: 'var(--text-3)', marginLeft: 4 }}>{tr(`（${count} 人）`, `(${count})`)}</span>
    </span>
  );
}

function ListingCard({ l, onOpen }: { l: SkillListingRead; onOpen: () => void }) {
  return (
    <button
      className="card"
      onClick={onOpen}
      style={{ display: 'block', width: '100%', textAlign: 'left', padding: '14px 16px', cursor: 'pointer' }}
    >
      <div className="row gap8" style={{ alignItems: 'center' }}>
        <span style={{ fontSize: 13.5, fontWeight: 650 }}>{l.skill?.name ?? tr('（技能已删除）', '(skill deleted)')}</span>
        {l.skill && <KindPill kind={l.skill.kind} />}
        <span className="pill sm" style={{ background: 'var(--surface-3)', color: 'var(--text-3)' }}>
          v{l.version ?? '—'}
        </span>
        <span style={{ marginLeft: 'auto', fontSize: 11, color: 'var(--text-3)' }}>{tr(`${l.install_count} 次安装`, `${l.install_count} installs`)}</span>
      </div>
      {l.summary && <div style={{ fontSize: 12, color: 'var(--text-2)', marginTop: 6, lineHeight: 1.5 }}>{l.summary}</div>}
      <div className="row gap8" style={{ marginTop: 8, alignItems: 'center' }}>
        <Stars avg={l.rating_avg} count={l.rating_count} />
        {(l.tags ?? []).map((t) => (
          <span key={t} className="pill sm" style={{ background: 'var(--accent-soft)', color: 'var(--accent-text)' }}>
            {t}
          </span>
        ))}
      </div>
    </button>
  );
}

function ListingDetailModal({ listingId, onClose }: { listingId: string; onClose: () => void }) {
  const queryClient = useQueryClient();
  const [myRating, setMyRating] = useState(0);
  const [myComment, setMyComment] = useState('');

  const { data: listing } = useQuery({
    queryKey: ['market-skill', listingId],
    queryFn: () => api.getMarketSkill(listingId),
  });
  const { data: reviews } = useQuery({
    queryKey: ['market-reviews', listingId],
    queryFn: () => api.listListingReviews(listingId),
  });

  const installMutation = useMutation({
    mutationFn: () => api.installMarketSkill(listingId),
    onSuccess: (skill) => {
      toast(`${tr('已安装到我的技能', 'Installed to my skills')}：${skill.name}`, 'ok');
      void queryClient.invalidateQueries({ queryKey: ['skills'] });
      void queryClient.invalidateQueries({ queryKey: ['market-skills'] });
    },
    onError: (e) => toast(`${tr('安装失败', 'Install failed')}：${errMsg(e)}`, 'error'),
  });
  const rateMutation = useMutation({
    mutationFn: () => api.addListingReview(listingId, { rating: myRating, comment: myComment.trim() || undefined }),
    onSuccess: () => {
      toast(tr('评分已提交', 'Rating submitted'), 'ok');
      setMyComment('');
      void queryClient.invalidateQueries({ queryKey: ['market-reviews', listingId] });
      void queryClient.invalidateQueries({ queryKey: ['market-skills'] });
    },
    onError: (e) => toast(`${tr('评分失败', 'Rating failed')}：${errMsg(e)}`, 'error'),
  });

  if (!listing) return null;
  return (
    <Modal
      open
      onClose={onClose}
      width={640}
      title={
        <>
          {listing.skill?.name ?? tr('技能', 'Skill')}
          {listing.skill && <KindPill kind={listing.skill.kind} />}
        </>
      }
      sub={`${listing.skill?.slug ?? ''} · ${tr(`发布版本 v${listing.version ?? '—'}`, `published v${listing.version ?? '—'}`)} · ${tr(`${listing.install_count} 次安装`, `${listing.install_count} installs`)}`}
      footer={
        <button className="btn btn-primary" disabled={installMutation.isPending} onClick={() => installMutation.mutate()}>
          <Icon name="download" size={13} /> {tr('安装到我的技能', 'Install to my skills')}
        </button>
      }
    >
      {listing.summary && <p style={{ fontSize: 12.5, color: 'var(--text-2)', marginBottom: 12 }}>{listing.summary}</p>}
      {listing.body && (
        <pre
          className="scroll"
          style={{
            whiteSpace: 'pre-wrap',
            fontSize: 12,
            lineHeight: 1.65,
            background: 'var(--surface-2)',
            border: '0.5px solid var(--border)',
            borderRadius: 8,
            padding: '12px 14px',
            maxHeight: 240,
            overflowY: 'auto',
          }}
        >
          {listing.body}
        </pre>
      )}

      {/* 我的评分 */}
      <div className="row gap8" style={{ marginTop: 14, alignItems: 'center' }}>
        {[1, 2, 3, 4, 5].map((n) => (
          <button
            key={n}
            className="icon-btn"
            style={{ width: 26, height: 26, color: n <= myRating ? 'var(--warn-tx)' : 'var(--text-3)', border: 'none', background: 'transparent', fontSize: 16 }}
            onClick={() => setMyRating(n)}
            aria-label={tr(`${n} 星`, `${n} stars`)}
          >
            {n <= myRating ? '★' : '☆'}
          </button>
        ))}
        <input
          className="input"
          style={{ flex: 1 }}
          placeholder={tr('用后感受（可选）', 'Your experience (optional)')}
          value={myComment}
          onChange={(e) => setMyComment(e.target.value)}
        />
        <button className="btn btn-soft" disabled={!myRating || rateMutation.isPending} onClick={() => rateMutation.mutate()}>
          {tr('提交评分', 'Submit rating')}
        </button>
      </div>

      {(reviews ?? []).length > 0 && (
        <div style={{ marginTop: 12 }}>
          {reviews!.map((r) => (
            <div key={r.id} className="row gap8" style={{ padding: '5px 0', alignItems: 'baseline' }}>
              <span style={{ fontSize: 11.5, color: 'var(--warn-tx)', flexShrink: 0 }}>{'★'.repeat(r.rating)}</span>
              <span style={{ fontSize: 12, color: 'var(--text-2)' }}>{r.comment ?? ''}</span>
            </div>
          ))}
        </div>
      )}
    </Modal>
  );
}

function MarketView() {
  const queryClient = useQueryClient();
  const [q, setQ] = useState('');
  const [sort, setSort] = useState<'-created_at' | 'installs'>('-created_at');
  const [openListing, setOpenListing] = useState<string | null>(null);

  const { data: me } = useQuery({ queryKey: ['me'], queryFn: () => api.me(), retry: false, staleTime: 60_000 });
  const admin = isAdmin(me);

  const { data: listings, isLoading, isError } = useQuery({
    queryKey: ['market-skills', sort, q],
    queryFn: () => api.listMarketSkills({ q: q || undefined, sort }),
    retry: false,
  });
  const { data: pending } = useQuery({
    queryKey: ['market-skills', 'pending'],
    queryFn: () => api.listMarketSkills({ status: 'pending' }),
    retry: false,
    enabled: admin,
  });

  const decideMutation = useMutation({
    mutationFn: ({ id, decision }: { id: string; decision: 'approve' | 'reject' }) => api.decideListing(id, decision),
    onSuccess: (listing) => {
      toast(listing.status === 'approved' ? tr('已上架', 'Listed') : tr('已驳回', 'Rejected'), 'ok');
      void queryClient.invalidateQueries({ queryKey: ['market-skills'] });
    },
    onError: (e) => toast(`${tr('审核失败', 'Review failed')}：${errMsg(e)}`, 'error'),
  });

  return (
    <div>
      <div className="row gap10" style={{ marginBottom: 16 }}>
        <Segmented
          options={[
            { v: '-created_at', label: tr('最新', 'Newest') },
            { v: 'installs', label: tr('最多安装', 'Most installed') },
          ]}
          value={sort}
          onChange={setSort}
        />
        <input className="input" style={{ width: 200, marginLeft: 'auto' }} placeholder={tr('搜索市场…', 'Search market…')} value={q} onChange={(e) => setQ(e.target.value)} />
      </div>

      {admin && (pending ?? []).length > 0 && (
        <div className="card" style={{ padding: '14px 16px', marginBottom: 14, borderLeft: '3px solid var(--warn-tx)' }}>
          <div style={{ fontSize: 13, fontWeight: 660, marginBottom: 8 }}>{tr('待审核（管理员）', 'Pending review (admin)')}</div>
          {pending!.map((l) => (
            <div key={l.id} className="row gap8" style={{ padding: '5px 0', alignItems: 'center' }}>
              <span style={{ fontSize: 12.5, fontWeight: 600 }}>{l.skill?.name}</span>
              <span style={{ fontSize: 11.5, color: 'var(--text-3)', flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                {l.summary}
              </span>
              <button className="btn btn-soft" style={{ fontSize: 11.5, padding: '3px 10px' }} onClick={() => decideMutation.mutate({ id: l.id, decision: 'approve' })}>
                {tr('通过', 'Approve')}
              </button>
              <button className="btn btn-ghost" style={{ fontSize: 11.5, padding: '3px 10px' }} onClick={() => decideMutation.mutate({ id: l.id, decision: 'reject' })}>
                {tr('驳回', 'Reject')}
              </button>
            </div>
          ))}
        </div>
      )}

      <div style={{ display: 'grid', gap: 10 }}>
        {isError ? (
          <EmptyState icon="sparkle" title={tr('市场加载失败', 'Failed to load market')} desc={tr('后端服务未启动或版本过旧', 'Backend not running or too old')} />
        ) : isLoading ? null : (listings ?? []).length === 0 ? (
          <EmptyState icon="sparkle" title={tr('市场还是空的', 'The market is empty')} desc={tr('在技能库把你的技能发布到市场，审核通过后即可安装', 'Publish your skills from the library; once approved they can be installed')} />
        ) : (
          listings!.map((l) => <ListingCard key={l.id} l={l} onOpen={() => setOpenListing(l.id)} />)
        )}
      </div>

      {openListing && <ListingDetailModal listingId={openListing} onClose={() => setOpenListing(null)} />}
    </div>
  );
}

// ---- 已启用面板 ----

function EnabledPanel({ projectId }: { projectId: string }) {
  const queryClient = useQueryClient();
  const { data: rows, isLoading } = useQuery({
    queryKey: ['project-skills', projectId],
    queryFn: () => api.listProjectSkills(projectId),
  });

  const patchMutation = useMutation({
    mutationFn: ({ id, enabled }: { id: string; enabled: boolean }) => api.patchProjectSkill(id, { enabled }),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ['project-skills', projectId] }),
    onError: (e) => toast(`${tr('操作失败', 'Action failed')}：${errMsg(e)}`, 'error'),
  });
  const removeMutation = useMutation({
    mutationFn: (id: string) => api.removeProjectSkill(id),
    onSuccess: () => {
      toast(tr('已从当前方向移除', 'Removed from current direction'), 'ok');
      void queryClient.invalidateQueries({ queryKey: ['project-skills', projectId] });
    },
    onError: (e) => toast(`${tr('移除失败', 'Remove failed')}：${errMsg(e)}`, 'error'),
  });

  const grouped = useMemo(() => {
    const m = new Map<string, ProjectSkillRead[]>();
    for (const r of rows ?? []) {
      const list = m.get(r.target) ?? [];
      list.push(r);
      m.set(r.target, list);
    }
    return [...m.entries()];
  }, [rows]);

  return (
    <div className="card" style={{ padding: '14px 16px' }}>
      <div style={{ fontSize: 13, fontWeight: 660, marginBottom: 4 }}>{tr('当前方向已启用', 'Enabled for current direction')}</div>
      <div style={{ fontSize: 11.5, color: 'var(--text-3)', marginBottom: 12 }}>
        {tr('新发起的 AI 任务会按下面的技能执行；进行中的任务不受影响', 'New AI tasks will use these skills; running tasks are unaffected')}
      </div>
      {isLoading ? null : grouped.length === 0 ? (
        <EmptyState compact icon="sparkle" title={tr('还没有启用技能', 'No skills enabled yet')} desc={tr('从左侧技能列表点启用到当前方向', 'Enable one from the skill list on the left')} />
      ) : (
        grouped.map(([target, list]) => (
          <div key={target} style={{ marginBottom: 12 }}>
            <div style={{ fontSize: 11, color: 'var(--text-3)', marginBottom: 6 }}>{skillTargetLabel(target)}</div>
            {list.map((r) => (
              <div key={r.id} className="row gap8" style={{ padding: '5px 0', alignItems: 'center' }}>
                <span
                  style={{
                    fontSize: 12.5,
                    fontWeight: 600,
                    color: r.enabled ? 'var(--text)' : 'var(--text-3)',
                    textDecoration: r.enabled ? 'none' : 'line-through',
                  }}
                >
                  {r.skill?.name ?? tr('（技能已删除）', '(skill deleted)')}
                </span>
                {r.pinned_version != null && (
                  <span className="pill sm" style={{ background: 'var(--surface-3)', color: 'var(--text-3)' }}>
                    v{r.pinned_version}
                  </span>
                )}
                <span className="row gap8" style={{ marginLeft: 'auto' }}>
                  <button
                    className="btn btn-ghost"
                    style={{ fontSize: 11.5, padding: '3px 8px' }}
                    onClick={() => patchMutation.mutate({ id: r.id, enabled: !r.enabled })}
                  >
                    {r.enabled ? tr('停用', 'Disable') : tr('启用', 'Enable')}
                  </button>
                  <button
                    className="icon-btn"
                    style={{ width: 24, height: 24 }}
                    title={tr('从当前方向移除', 'Remove from current direction')}
                    onClick={() => removeMutation.mutate(r.id)}
                  >
                    <Icon name="x" size={12} />
                  </button>
                </span>
              </div>
            ))}
          </div>
        ))
      )}
    </div>
  );
}

// ---- 页面 ----

export function SkillsPage() {
  const queryClient = useQueryClient();
  const { currentProjectId } = useProject();
  const [view, setView] = useState<'library' | 'market'>('library');
  const [scope, setScope] = useState<ScopeFilter>('all');
  const [kind, setKind] = useState<KindFilter>('all');
  const [q, setQ] = useState('');
  const [openId, setOpenId] = useState<string | null>(null);
  const [createOpen, setCreateOpen] = useState(false);
  const importInputRef = useRef<HTMLInputElement>(null);

  const { data: skills, isLoading, isError } = useQuery({
    queryKey: ['skills'],
    queryFn: () => api.listSkills(),
    retry: false,
  });

  const importMutation = useMutation({
    mutationFn: (data: SkillExportData) => api.importSkill(data),
    onSuccess: (s) => {
      toast(`${tr('已导入', 'Imported')}：${s.name}`, 'ok');
      void queryClient.invalidateQueries({ queryKey: ['skills'] });
      setOpenId(s.id);
    },
    onError: (e) => toast(`${tr('导入失败', 'Import failed')}：${errMsg(e)}`, 'error'),
  });

  async function onImportFile(file: File | undefined): Promise<void> {
    if (!file) return;
    try {
      const data = JSON.parse(await file.text()) as SkillExportData;
      importMutation.mutate(data);
    } catch {
      toast(tr('不是合法的技能包文件（JSON 解析失败）', 'Not a valid skill package (JSON parse failed)'), 'error');
    }
  }

  const filtered = useMemo(() => {
    const kw = q.trim().toLowerCase();
    return (skills ?? []).filter((s) => {
      if (scope === 'builtin' && s.scope !== 'builtin') return false;
      if (scope === 'mine' && s.scope === 'builtin') return false;
      if (kind !== 'all' && s.kind !== kind) return false;
      if (kw && !`${s.name} ${s.slug} ${s.description ?? ''}`.toLowerCase().includes(kw)) return false;
      return true;
    });
  }, [skills, scope, kind, q]);

  return (
    <div className="page fadeup">
      <PageHead
        eyebrow="Polaris"
        title={tr('技能', 'Skills')}
        sub={tr('为各环节 AI 定制判断标准、评审人设与流程模板', 'Custom criteria, reviewer personas and workflow templates for each stage')}
        right={
          <>
            <Segmented
              options={[
                { v: 'library', label: tr('技能库', 'Library') },
                { v: 'market', label: tr('技能市场', 'Market') },
              ]}
              value={view}
              onChange={setView}
            />
            {view === 'library' && (
              <>
                <input
                  ref={importInputRef}
                  type="file"
                  accept=".json,application/json"
                  style={{ display: 'none' }}
                  onChange={(e) => {
                    void onImportFile(e.target.files?.[0]);
                    e.target.value = '';
                  }}
                />
                <button className="btn btn-soft" disabled={importMutation.isPending} onClick={() => importInputRef.current?.click()}>
                  {tr('导入', 'Import')}
                </button>
                <button className="btn btn-primary" onClick={() => setCreateOpen(true)}>
                  <Icon name="plus" size={14} /> {tr('新建技能', 'New skill')}
                </button>
              </>
            )}
          </>
        }
      />

      {view === 'market' && <MarketView />}

      <div className="row gap10" style={{ marginBottom: 16, flexWrap: 'wrap', display: view === 'library' ? undefined : 'none' }}>
        <Segmented
          options={[
            { v: 'all', label: tr('全部', 'All') },
            { v: 'builtin', label: tr('内置', 'Built-in') },
            { v: 'mine', label: tr('我的', 'Mine') },
          ]}
          value={scope}
          onChange={setScope}
        />
        <Segmented options={KIND_FILTERS.map((f) => ({ v: f.v, label: tr(f.zh, f.en) }))} value={kind} onChange={setKind} />
        <input
          className="input"
          style={{ width: 200, marginLeft: 'auto' }}
          placeholder={tr('搜索技能…', 'Search skills…')}
          value={q}
          onChange={(e) => setQ(e.target.value)}
        />
      </div>

      <div
        style={{
          display: view === 'library' ? 'grid' : 'none',
          gridTemplateColumns: 'minmax(0, 1fr) 320px',
          gap: 18,
          alignItems: 'start',
        }}
      >
        <div style={{ display: 'grid', gap: 10 }}>
          {isError ? (
            <EmptyState icon="sparkle" title={tr('技能列表加载失败', 'Failed to load skills')} desc={tr('后端服务未启动或版本过旧', 'Backend not running or too old')} />
          ) : isLoading ? null : filtered.length === 0 ? (
            <EmptyState
              icon="sparkle"
              title={tr('没有匹配的技能', 'No matching skills')}
              desc={tr('换个筛选条件，或新建一个技能', 'Adjust the filters or create a new skill')}
              action={
                <button className="btn btn-primary" onClick={() => setCreateOpen(true)}>
                  {tr('新建技能', 'New skill')}
                </button>
              }
            />
          ) : (
            filtered.map((s) => <SkillCard key={s.id} s={s} onOpen={() => setOpenId(s.id)} />)
          )}
        </div>

        {currentProjectId ? (
          <EnabledPanel projectId={currentProjectId} />
        ) : (
          <div className="card" style={{ padding: '14px 16px' }}>
            <EmptyState compact icon="compass" title={tr('未选择研究方向', 'No direction selected')} desc={tr('选择研究方向后可把技能启用到该方向', 'Pick a research direction to enable skills for it')} />
          </div>
        )}
      </div>

      {openId && <SkillDetailModal skillId={openId} onClose={() => setOpenId(null)} />}
      {createOpen && <SkillCreateModal onClose={() => setCreateOpen(false)} onCreated={(s) => setOpenId(s.id)} />}
    </div>
  );
}
