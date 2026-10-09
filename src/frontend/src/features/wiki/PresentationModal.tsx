import { useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useMutation, useQuery } from '@tanstack/react-query';
import { Modal } from '../../components/ui/Modal';
import { Segmented } from '../../components/ui/Segmented';
import { FormField } from '../../components/ui/FormField';
import { EmptyState } from '../../components/ui/EmptyState';
import { toast } from '../../components/ui/Toast';
import { api } from '../../lib/api';
import { tr } from '../../lib/i18n';

/* ============================================================
   论文分享 PPT 弹窗（文献追踪板块）：
   - 单篇分享：选 1 篇 → 生成单篇讲解 PPT；
   - 多篇梳理：选 2-12 篇 → 生成主题线梳理 PPT。
   生成走 AI 任务（kind=presentation），完成后任务详情页可下载。
   建议选已编译（有 AI 精读介绍）的论文，内容会更充实。
   ============================================================ */

type Mode = 'single' | 'survey';

export function PresentationModal({
  projectId,
  initialPaperId,
  onClose,
}: {
  projectId: string;
  /** 从论文行进入时预选该论文 */
  initialPaperId?: string;
  onClose: () => void;
}) {
  const navigate = useNavigate();
  const [mode, setMode] = useState<Mode>('single');
  const [selected, setSelected] = useState<string[]>(initialPaperId ? [initialPaperId] : []);
  const [notes, setNotes] = useState('');
  const [q, setQ] = useState('');

  const { data, isLoading } = useQuery({
    queryKey: ['papers', projectId, 'present-pick'],
    queryFn: () => api.listPapers(projectId, { size: 100, sort: 'relevance' }),
  });

  const papers = useMemo(() => {
    const kw = q.trim().toLowerCase();
    return (data?.items ?? []).filter(
      (p) => p.status !== 'excluded' && (!kw || p.title.toLowerCase().includes(kw)),
    );
  }, [data, q]);

  const createMutation = useMutation({
    mutationFn: () =>
      api.createPresentation(projectId, {
        paper_ids: selected,
        mode,
        notes: notes.trim() || undefined,
      }),
    onSuccess: (run) => {
      toast(
        tr('已开始生成 PPT，完成后在任务详情中下载', 'Generating slides. Download them from the task when done.'),
        'ok',
      );
      onClose();
      navigate(`/voyages/${run.id}`);
    },
    onError: (e) =>
      toast(`${tr('无法开始：', 'Couldn’t start: ')}${e instanceof Error ? e.message : String(e)}`, 'error'),
  });

  function toggle(id: string) {
    setSelected((prev) => {
      if (mode === 'single') return prev.includes(id) ? [] : [id];
      return prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id].slice(0, 12);
    });
  }

  const canSubmit =
    mode === 'single' ? selected.length === 1 : selected.length >= 2 && selected.length <= 12;

  return (
    <Modal
      open
      onClose={onClose}
      width={640}
      title={tr('生成论文分享 PPT', 'Generate paper slides')}
      sub={tr(
        '使用内置模板，可在技能页调整',
        'Uses the built-in template. You can change it on the Skills page.',
      )}
      footer={
        <>
          <span style={{ marginRight: 'auto', fontSize: 11.5, color: 'var(--text-3)' }}>
            {tr(`已选 ${selected.length} 篇`, `${selected.length} selected`)}
            {mode === 'survey' && selected.length < 2
              ? tr('，多篇梳理至少选 2 篇', '. A survey needs at least 2.')
              : ''}
          </span>
          <button className="btn btn-ghost" onClick={onClose}>
            {tr('取消', 'Cancel')}
          </button>
          <button
            className="btn btn-primary"
            disabled={!canSubmit || createMutation.isPending}
            onClick={() => createMutation.mutate()}
          >
            {createMutation.isPending ? tr('正在开始…', 'Starting…') : tr('生成 PPT', 'Generate PPT')}
          </button>
        </>
      }
    >
      <div className="row gap10" style={{ marginBottom: 12 }}>
        <Segmented<Mode>
          options={[
            { v: 'single', label: tr('单篇分享', 'Single paper') },
            { v: 'survey', label: tr('多篇梳理', 'Survey') },
          ]}
          value={mode}
          onChange={(m) => {
            setMode(m);
            if (m === 'single' && selected.length > 1) setSelected(selected.slice(0, 1));
          }}
        />
        <input
          className="input"
          style={{ flex: 1 }}
          placeholder={tr('搜索论文标题…', 'Search paper titles…')}
          value={q}
          onChange={(e) => setQ(e.target.value)}
        />
      </div>

      <div
        className="scroll"
        style={{
          maxHeight: 260,
          overflowY: 'auto',
          border: '0.5px solid var(--border)',
          borderRadius: 8,
          marginBottom: 12,
        }}
      >
        {isLoading ? null : papers.length === 0 ? (
          <EmptyState
            compact
            icon="book"
            title={tr('没有可选论文', 'No papers to pick')}
            desc={tr('先在「检索与同步」中添加论文。', 'Add papers under “Search & sync” first.')}
          />
        ) : (
          papers.map((p) => {
            const on = selected.includes(p.id);
            return (
              <label
                key={p.id}
                className="row gap8"
                style={{
                  padding: '8px 12px',
                  cursor: 'pointer',
                  background: on ? 'var(--accent-soft)' : 'transparent',
                  borderBottom: '0.5px solid var(--border)',
                }}
              >
                <input type="checkbox" checked={on} onChange={() => toggle(p.id)} />
                <span style={{ flex: 1, fontSize: 12.5, lineHeight: 1.4 }}>
                  {p.title}
                  <span style={{ color: 'var(--text-3)', marginLeft: 6, fontSize: 11 }}>
                    {p.year ?? ''}
                  </span>
                </span>
                {p.status === 'compiled' && (
                  <span
                    className="pill sm"
                    style={{ background: 'var(--ok-bg)', color: 'var(--ok-tx)', flexShrink: 0 }}
                  >
                    {tr('已解读', 'Summarized')}
                  </span>
                )}
              </label>
            );
          })
        )}
      </div>

      <FormField
        label={tr('讲者备注', 'Speaker notes')}
        hint={tr('可选，例如听众背景和重点', 'Optional. Audience and focus, for example.')}
      >
        <textarea
          className="textarea"
          rows={2}
          placeholder={tr(
            '例如 组会分享，听众了解 LLM 基础，重点讲训练闭环',
            'e.g. Lab meeting, audience knows LLM basics, focus on the training loop',
          )}
          value={notes}
          onChange={(e) => setNotes(e.target.value)}
        />
      </FormField>
      <p style={{ fontSize: 11.5, color: 'var(--text-3)', marginTop: 8 }}>
        {tr(
          '已解读的论文生成的 PPT 内容和配图更完整。',
          'Summarized papers give fuller slides with figures.',
        )}
      </p>
    </Modal>
  );
}
