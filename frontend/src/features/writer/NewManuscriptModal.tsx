import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Icon } from '../../components/ui/Icon';
import { Modal } from '../../components/ui/Modal';
import { FormField } from '../../components/ui/FormField';
import { toast } from '../../components/ui/Toast';
import { api } from '../../lib/api';
import { tr } from '../../lib/i18n';

/* ============================================================
   「新建论文草稿」Modal：标题 + 模板（GET /manuscripts/templates）
   + 可选关联 idea（promoted）与 experiment（done）。
   创建成功后进入编辑工作台 /writer/:id。
   ============================================================ */

export interface NewManuscriptModalProps {
  open: boolean;
  onClose: () => void;
  pid: string;
}

export function NewManuscriptModal({ open, onClose, pid }: NewManuscriptModalProps) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  const [title, setTitle] = useState('');
  const [template, setTemplate] = useState('');
  const [ideaId, setIdeaId] = useState('');
  const [experimentId, setExperimentId] = useState('');

  const templatesQuery = useQuery({
    queryKey: ['manuscript-templates'],
    queryFn: () => api.listManuscriptTemplates(),
    enabled: open,
    retry: false,
    staleTime: 5 * 60_000,
  });
  const ideasQuery = useQuery({
    queryKey: ['ideas', pid, 'promoted'],
    queryFn: () => api.listIdeas(pid, { status: 'promoted' }),
    enabled: open && !!pid,
    retry: false,
  });
  const expsQuery = useQuery({
    queryKey: ['experiments', pid],
    queryFn: () => api.listExperiments(pid),
    enabled: open && !!pid,
    retry: false,
  });

  const templates = templatesQuery.data ?? [];
  const ideas = ideasQuery.data ?? [];
  const doneExps = (expsQuery.data ?? []).filter((e) => e.status === 'done');

  // 打开时重置；模板列表加载后默认选第一个
  useEffect(() => {
    if (!open) return;
    setTitle('');
    setIdeaId('');
    setExperimentId('');
  }, [open]);
  useEffect(() => {
    if (!open || templates.length === 0) return;
    if (!templates.some((t) => t.key === template)) setTemplate(templates[0]!.key);
  }, [open, templates, template]);

  const mutation = useMutation({
    mutationFn: () =>
      api.createManuscript(pid, {
        title: title.trim(),
        template,
        ...(ideaId ? { idea_id: ideaId } : {}),
        ...(experimentId ? { experiment_id: experimentId } : {}),
      }),
    onSuccess: (m) => {
      toast(tr('论文草稿已创建', 'Manuscript created'), 'ok');
      void queryClient.invalidateQueries({ queryKey: ['manuscripts', pid] });
      onClose();
      navigate(`/writer/${m.id}`);
    },
    onError: (e) => toast(`${tr('创建失败：', 'Create failed: ')}${e instanceof Error ? e.message : String(e)}`, 'error'),
  });

  const canSubmit = !!title.trim() && !!template && !mutation.isPending;

  return (
    <Modal
      open={open}
      onClose={onClose}
      width={560}
      title={
        <>
          <Icon name="pen" size={16} style={{ color: 'var(--accent)' }} />
          {tr('新建论文草稿', 'New manuscript')}
        </>
      }
      sub={tr('从会议模板展开文件，并自动从关联实验与文献库组装事实包', 'Expands files from a venue template and assembles a fact pack from the linked experiment and library')}
      footer={
        <>
          <button className="btn btn-ghost" onClick={onClose}>{tr('取消', 'Cancel')}</button>
          <button className="btn btn-primary" disabled={!canSubmit} onClick={() => mutation.mutate()}>
            {mutation.isPending ? (
              <>
                <Icon name="refresh" size={14} style={{ animation: 'spin 1s linear infinite' }} />
                {tr('创建中…', 'Creating…')}
              </>
            ) : (
              <>
                <Icon name="plus" size={14} />
                {tr('创建草稿', 'Create manuscript')}
              </>
            )}
          </button>
        </>
      }
    >
      <FormField label={tr('论文标题', 'Title')}>
        <input
          className="input"
          autoFocus
          value={title}
          onChange={(e) => setTitle(e.target.value)}
          placeholder={tr('论文工作标题（之后可以改）', 'Working title (you can change it later)')}
        />
      </FormField>

      <FormField
        label={tr('会议模板', 'Venue template')}
        error={templatesQuery.isError ? tr('无法加载模板列表（后端不可用或接口尚未就绪）。', 'Failed to load templates (backend unavailable or API not ready).') : null}
        hint={tr('模板为平台内置的近似排版（非会议官方模板包），正式投稿前请换用官方模板核对格式。', 'Templates are built-in approximations, not official venue packages — switch to the official template and check formatting before submitting.')}
      >
        <select
          className="input"
          value={template}
          onChange={(e) => setTemplate(e.target.value)}
          disabled={templates.length === 0}
        >
          {templates.length === 0 && (
            <option value="">{templatesQuery.isLoading ? tr('加载中…', 'Loading…') : tr('（暂无模板）', '(no templates)')}</option>
          )}
          {templates.map((t) => (
            <option key={t.key} value={t.key}>
              {t.name}
              {t.page_limit ? tr(`（正文 ≤${t.page_limit} 页）`, ` (body ≤${t.page_limit} pages)`) : ''}
            </option>
          ))}
        </select>
      </FormField>

      <div className="row gap12" style={{ alignItems: 'flex-start' }}>
        <FormField
          label={tr('关联想法（可选）', 'Linked idea (optional)')}
          style={{ flex: 1 }}
          hint={tr('论文事实包中研究想法分区的来源；仅列出已晋级的想法。', 'Source for the idea section of the fact pack; only promoted ideas are listed.')}
        >
          <select className="input" value={ideaId} onChange={(e) => setIdeaId(e.target.value)}>
            <option value="">
              {ideasQuery.isLoading ? tr('加载中…', 'Loading…') : tr('— 不关联 —', '— none —')}
            </option>
            {ideas.map((i) => (
              <option key={i.id} value={i.id}>{i.title}</option>
            ))}
          </select>
        </FormField>
        <FormField
          label={tr('关联实验（可选）', 'Linked experiment (optional)')}
          style={{ flex: 1 }}
          hint={tr('事实包的指标 / 图表 / 假设来源；仅列已完成实验。', 'Source of fact-pack metrics / figures / hypotheses; only finished experiments are listed.')}
        >
          <select className="input" value={experimentId} onChange={(e) => setExperimentId(e.target.value)}>
            <option value="">
              {expsQuery.isLoading ? tr('加载中…', 'Loading…') : tr('— 不关联 —', '— none —')}
            </option>
            {doneExps.map((x) => (
              <option key={x.id} value={x.id}>{x.idea_title}</option>
            ))}
          </select>
        </FormField>
      </div>
    </Modal>
  );
}
