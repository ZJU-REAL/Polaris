import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useQueryClient } from '@tanstack/react-query';
import { Icon } from '../../components/ui/Icon';
import { PageHead } from '../../components/ui/PageHead';
import { FormField } from '../../components/ui/FormField';
import { toast } from '../../components/ui/Toast';
import { topicPath, useProject } from '../../app/project';
import { api } from '../../lib/api';
import { tr } from '../../lib/i18n';
import { useLibraries } from '../libraries/hooks';
import { LibraryPicker } from '../libraries/LibraryPicker';

/* ============================================================
   /projects/new — 建课题（30 秒）。
   只有三件事：课题名称 + 一句话 statement + 关联哪些已有文献库（可跳过）。
   收录配置（rubric / 关键词 / arXiv 分类 / 节奏 / 锚点 …）属于文献库，在
   建库表单里填、由文献库管理员维护；建课题不碰这些。
   创建成功后进课题工作台。
   ============================================================ */

export function ProjectWizardPage() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { setCurrentProjectId } = useProject();

  const [name, setName] = useState('');
  const [statement, setStatement] = useState('');
  const [formError, setFormError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  // 只能关联已激活（active）的文献库；pending/rejected 不作为语料
  const librariesQuery = useLibraries();
  const libraries = (librariesQuery.data ?? []).filter((l) => l.status === 'active');
  const [selectedLibraryIds, setSelectedLibraryIds] = useState<Set<string>>(new Set());
  function toggleLibrary(libId: string) {
    setSelectedLibraryIds((prev) => {
      const next = new Set(prev);
      if (next.has(libId)) next.delete(libId);
      else next.add(libId);
      return next;
    });
  }

  /** 创建课题：名称 + 一句话 + 关联库，一次调用；成功后进课题工作台。 */
  async function create() {
    if (!name.trim()) {
      setFormError(tr('请填写课题名称', 'Please enter a topic name'));
      return;
    }
    setFormError(null);
    setSubmitting(true);
    try {
      const created = await api.createProject({
        name: name.trim(),
        statement: statement.trim() || undefined,
        source_library_ids: [...selectedLibraryIds],
      });
      await queryClient.invalidateQueries({ queryKey: ['projects'] });
      setCurrentProjectId(created.id);
      toast(tr('课题已创建', 'Topic created'), 'ok');
      navigate(topicPath(created.id));
    } catch (e) {
      toast(`${tr('创建失败：', 'Create failed: ')}${e instanceof Error ? e.message : String(e)}`, 'error');
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="page fadeup" style={{ maxWidth: 720 }}>
      <PageHead
        eyebrow="Polaris · Topics"
        title={tr('新建课题', 'New topic')}
        sub={tr(
          '名称 + 一句话即可创建；再选几个文献库作为语料（可跳过，稍后在课题设置里加）。',
          'Just a name and one sentence to create — then pick a few libraries as your corpus (optional, add later in topic settings).',
        )}
      />

      {/* —— 名称 + 一句话 —— */}
      <div className="card card-pad" style={{ marginBottom: 12 }}>
        <FormField label={tr('课题名称', 'Name')} hint={tr('将显示在侧栏与课题列表中', 'Shown in the sidebar and topic list')}>
          <input className="input" value={name} onChange={(e) => setName(e.target.value)}
            placeholder={tr('如：LLM 自主科研智能体', 'e.g. LLM autonomous research agents')} />
        </FormField>
        <FormField
          label={tr('一句话定义', 'Statement')}
          hint={tr('用一句话说清这个课题研究什么、为什么重要（可选）', 'One sentence on what this topic studies and why it matters (optional)')}
        >
          <textarea className="textarea" rows={3} value={statement} onChange={(e) => setStatement(e.target.value)}
            placeholder={tr('如：让 LLM agent 端到端完成从文献调研到论文的研究方法与系统', 'e.g. Methods and systems for LLM agents to go end-to-end from literature survey to paper')} />
        </FormField>
        {formError && <div className="field-error" style={{ marginTop: 4 }}>{formError}</div>}
      </div>

      {/* —— 关联文献库（可选，可全部不选） —— */}
      <div className="card card-pad" style={{ marginBottom: 12 }}>
        <div className="row" style={{ justifyContent: 'space-between', marginBottom: 4 }}>
          <span className="section-h">
            <Icon name="book" size={15} style={{ color: 'var(--accent)' }} />
            {tr('关联文献库', 'Linked libraries')}
          </span>
          {libraries.length > 0 && (
            <span style={{ fontSize: 11.5, color: 'var(--text-3)' }}>
              {tr(`已选 ${selectedLibraryIds.size} 个`, `${selectedLibraryIds.size} selected`)}
            </span>
          )}
        </div>
        <div style={{ fontSize: 12.5, color: 'var(--text-3)', margin: '2px 0 12px', lineHeight: 1.6 }}>
          {tr(
            '课题的语料 = 关联文献库的并集；文献库由文献库管理员维护，可全部不选跳过。',
            'A topic’s corpus is the union of its linked libraries, maintained by library admins — you can skip this.',
          )}
        </div>
        {librariesQuery.isLoading ? (
          <div className="col gap8">
            {[0, 1].map((i) => (
              <div key={i} className="skel" style={{ height: 64, borderRadius: 12 }} />
            ))}
          </div>
        ) : libraries.length === 0 ? (
          <div className="empty" style={{ padding: '20px 14px', textAlign: 'center' }}>
            <div style={{ marginBottom: 10, color: 'var(--text-3)', lineHeight: 1.6 }}>
              {tr('还没有可关联的文献库。', 'No libraries available to link yet.')}
            </div>
            <button className="btn btn-soft sm" onClick={() => navigate('/libraries')}>
              <Icon name="book" size={13} />
              {tr('去「文献库」新建一个（需管理员审批）', 'Create one under Libraries (needs admin approval)')}
            </button>
          </div>
        ) : (
          <LibraryPicker libraries={libraries} selectedIds={selectedLibraryIds} onToggle={toggleLibrary} />
        )}
      </div>

      {/* 底部操作栏 */}
      <div className="row gap10" style={{ marginTop: 18, justifyContent: 'flex-end' }}>
        <button className="btn btn-primary" onClick={() => void create()} disabled={submitting}>
          <Icon name="check" size={14} />
          {submitting ? tr('创建中…', 'Creating…') : tr('创建课题', 'Create topic')}
        </button>
      </div>
    </div>
  );
}
