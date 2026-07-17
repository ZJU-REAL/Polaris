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
        tr('PPT 生成任务已发起，完成后可在任务详情页下载', 'PPT task started — download it from the task detail page when done'),
        'ok',
      );
      onClose();
      navigate(`/voyages/${run.id}`);
    },
    onError: (e) =>
      toast(`${tr('发起失败：', 'Failed to start: ')}${e instanceof Error ? e.message : String(e)}`, 'error'),
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
      title={tr('生成论文分享 PPT', 'Generate paper sharing PPT')}
      sub={tr(
        '按实验室模板生成，可在技能页调整论文分享 PPT 制作技能的规范',
        'Generated from the lab template — tweak the PPT skill rules on the Skills page',
      )}
      footer={
        <>
          <span style={{ marginRight: 'auto', fontSize: 11.5, color: 'var(--text-3)' }}>
            {tr(`已选 ${selected.length} 篇`, `${selected.length} selected`)}
            {mode === 'survey' && selected.length < 2
              ? tr('（梳理模式至少选 2 篇）', '(survey mode needs at least 2)')
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
            {createMutation.isPending ? tr('发起中…', 'Starting…') : tr('生成 PPT', 'Generate PPT')}
          </button>
        </>
      }
    >
      <div className="row gap10" style={{ marginBottom: 12 }}>
        <Segmented<Mode>
          options={[
            { v: 'single', label: tr('单篇分享', 'Single paper') },
            { v: 'survey', label: tr('多篇梳理', 'Multi-paper survey') },
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
            desc={tr('先在建库与同步里收录论文', 'Add papers via Ingest & sync first')}
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
                    {tr('已精读', 'Compiled')}
                  </span>
                )}
              </label>
            );
          })
        )}
      </div>

      <FormField
        label={tr('讲者备注', 'Speaker notes')}
        hint={tr(
          '可选：听众背景、要突出的侧重点，AI 会照顾到',
          'Optional: audience background and points to highlight — the AI will take them into account',
        )}
      >
        <textarea
          className="textarea"
          rows={2}
          placeholder={tr(
            '例如：面向组会分享，听众了解 LLM 基础，重点讲清训练闭环',
            'e.g. lab meeting talk, audience knows LLM basics, focus on the training loop',
          )}
          value={notes}
          onChange={(e) => setNotes(e.target.value)}
        />
      </FormField>
      <p style={{ fontSize: 11.5, color: 'var(--text-3)', marginTop: 8 }}>
        {tr(
          '提示：优先选择已精读的论文，PPT 内容与配图会更充实。',
          'Tip: prefer compiled papers — the slides get richer content and figures.',
        )}
      </p>
    </Modal>
  );
}
